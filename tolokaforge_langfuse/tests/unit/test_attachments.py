"""The post-trial attachment step (ADR-0047 amendment): the attachment set, the compression rule,
the data-safety scan, manifest v2, and the Langfuse media / ingestion calls against a fake
transport."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from tolokaforge_langfuse.attachments import (
    ATTACH_ALL,
    ATTACH_CORE,
    ATTACH_NONE,
    SecretScan,
    allowed_attachment,
    attachment_names,
    build_manifest,
    encode,
    list_trial_files,
    plan_attachments,
)
from tolokaforge_langfuse.media import (
    AttachCounts,
    LangfuseAttachments,
    api_base_from_endpoint,
)

pytestmark = pytest.mark.unit

V1_FILES = (
    "env.yaml",
    "grade.yaml",
    "logs.yaml",
    "metrics.yaml",
    "prompts.yaml",
    "task.yaml",
    "tools_schemas.yaml",
    "trajectory.yaml",
)
V3_FILES = V1_FILES + ("judge_inputs.yaml", "judge_trajectory.yaml", "tool_log.yaml")
T0 = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)


def write_trial(trial_dir: Path, names: tuple[str, ...]) -> Path:
    trial_dir.mkdir(parents=True)
    for name in names:
        (trial_dir / name).write_bytes(f"{name}: content\n".encode())
    (trial_dir / "video").mkdir()
    (trial_dir / "video" / "trial.mp4").write_bytes(b"\x00\x01")
    (trial_dir / "services").mkdir()
    (trial_dir / "services" / "db.log").write_text("db started\n")
    (trial_dir / ".DS_Store").write_bytes(b"junk")
    return trial_dir


class TestAttachmentSet:
    def test_v1_and_v3_trials_attach_their_top_level_files_only(self, tmp_path: Path) -> None:
        v1 = write_trial(tmp_path / "v1" / "trials" / "T" / "0", V1_FILES)
        v3 = write_trial(tmp_path / "v3" / "trials" / "T" / "0", V3_FILES)
        assert [p.name for p in list_trial_files(v1)] == sorted(V1_FILES)
        assert attachment_names(v3, ATTACH_ALL) == sorted(V3_FILES)
        assert attachment_names(v3, ATTACH_CORE) == [
            "grade.yaml",
            "logs.yaml",
            "prompts.yaml",
            "task.yaml",
            "tools_schemas.yaml",
        ]
        assert attachment_names(v3, ATTACH_NONE) == []
        assert list_trial_files(tmp_path / "missing") == []
        with pytest.raises(ValueError):
            attachment_names(v1, "everything")

    def test_compression_rule_and_both_hashes(self, tmp_path: Path) -> None:
        trial = write_trial(tmp_path / "trials" / "T" / "0", V1_FILES)
        files = {f.name: f for f in plan_attachments(trial, ATTACH_ALL)}
        for name in ("env.yaml", "trajectory.yaml"):
            stored = files[name]
            assert stored.encoding == "gzip" and stored.content_type == "application/gzip"
            assert gzip.decompress(stored.payload) == stored.original == (trial / name).read_bytes()
            assert stored.sha256 == hashlib.sha256(stored.original).hexdigest()
            assert stored.stored_sha256 == hashlib.sha256(stored.payload).hexdigest()
            assert stored.sha256 != stored.stored_sha256
        assert files["metrics.yaml"].encoding == "none"
        assert files["metrics.yaml"].sha256 == files["metrics.yaml"].stored_sha256
        assert files["prompts.yaml"].content_type == "text/plain"
        assert files["task.yaml"].content_type == "application/x-yaml"
        # deterministic gzip: the same bytes give the same stored hash on every run
        assert (
            encode("env.yaml", b"a: 1\n").stored_sha256
            == encode("env.yaml", b"a: 1\n").stored_sha256
        )
        assert allowed_attachment("task.yaml") and not allowed_attachment("trial.mp4")


class TestSecretScan:
    @pytest.mark.parametrize(
        "payload, rule",
        [
            (b"OPENROUTER_API_KEY=sk-or-v1-0123456789abcdef0123456789abcdef", "openrouter-key"),
            (b"Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", "authorization-header"),
            (b"-----BEGIN RSA PRIVATE KEY-----\nabc", "pem-private-key"),
            (b"postgresql://app:hunter22@db:5432/x", "url-credentials"),
            (b"pk-lf-12345678-1234-1234-1234-123456789abc", "langfuse-key"),
            (b"ghp_" + b"a" * 36, "github-token"),
        ],
    )
    def test_key_shapes_are_found_and_masked(self, payload: bytes, rule: str) -> None:
        findings = SecretScan().scan(payload)
        assert any(f.startswith(rule) for f in findings), findings
        assert not any(payload[-8:].decode() in f for f in findings)  # never the tail

    def test_known_values_and_clean_payloads(self) -> None:
        scan = SecretScan(known_values=["process-held-secret-value-123", "short"])
        (finding,) = scan.scan(b"the config says process-held-secret-value-123 here")
        assert finding == "known-secret-value (**** (29 chars))"  # never a head of a credential
        assert scan.scan(b"short is too short to count") == []
        assert scan.scan(b"postgresql://app:***@db:5432/x api_key: null token: <redacted>") == []
        assert scan.scan(b"messages:\n- role: user\n  content: please book seat 12A\n") == []

    def test_long_lowercase_runs_scan_in_linear_time(self) -> None:
        import time

        payload = b"a" * 200_000 + b" fine"
        started = time.perf_counter()
        assert SecretScan().scan(payload) == []
        assert time.perf_counter() - started < 0.5
        assert (
            SecretScan()
            .scan(b"see postgresql://app:s3cretpass@db:5432/x")[0]
            .startswith("url-credentials")
        )


class TestManifest:
    def test_manifest_v2_shape_and_completeness(self, tmp_path: Path) -> None:
        trial = write_trial(tmp_path / "trials" / "T" / "0", V1_FILES)
        attached = []
        from tolokaforge_langfuse.attachments import AttachedFile

        for file in plan_attachments(trial, ATTACH_ALL):
            attached.append(
                AttachedFile(file=file, media_id=f"m-{file.name}", token=f"@@@{file.name}@@@")
            )
        manifest = build_manifest(trial, attached, [])
        assert manifest["attachments_schema"] == 2
        assert manifest["attachments_complete"] is True
        assert manifest["attachments_skipped"] == []
        assert list(manifest["attachments"]) == sorted(V1_FILES)
        env = manifest["attachments"]["env.yaml"]
        assert set(env) == {
            "media_id",
            "media",
            "sha256",
            "stored_sha256",
            "bytes",
            "stored_bytes",
            "content_type",
            "encoding",
        }
        assert env["encoding"] == "gzip" and env["bytes"] > env["stored_bytes"] - 40
        partial = build_manifest(trial, attached[:-1], [{"name": "trajectory.yaml", "rule": "x"}])
        assert partial["attachments_complete"] is False
        assert build_manifest(trial, attached[:-1], [])["attachments_complete"] is False


class _FakeLangfuse:
    """Answers the media and ingestion calls like Langfuse 3.205.1 does; records everything."""

    def __init__(self, *, azure: bool = True, fail_put: bool = False) -> None:
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.media: dict[str, str] = {}  # sha256 b64 -> media id
        self.stored: set[str] = set()
        self.blobs: dict[str, bytes] = {}
        self.azure = azure
        self.fail_put = fail_put
        self.host = (
            "https://acct.blob.core.windows.net/media" if azure else "https://s3.example/media"
        )

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        if method == "POST" and url.endswith("/api/public/media"):
            request = json.loads(body)
            sha = request["sha256Hash"]
            media_id = self.media.setdefault(sha, f"m{len(self.media) + 1}")
            answer = {"mediaId": media_id}
            if sha not in self.stored:
                answer["uploadUrl"] = f"{self.host}/{media_id}?sig=x"
            return 201, json.dumps(answer).encode()
        if method == "PUT":
            if self.fail_put:
                return 400, b"MissingRequiredHeader"
            media_id = url.rsplit("/", 1)[1].split("?")[0]
            self.blobs[media_id] = body
            return 201, b""
        if method == "PATCH":
            media_id = url.rsplit("/", 1)[1]
            for sha, mid in self.media.items():
                if mid == media_id:
                    self.stored.add(sha)
            return 200, b"{}"
        if method == "POST" and url.endswith("/api/public/ingestion"):
            return (
                207,
                json.dumps({"successes": [{"id": "x", "status": 201}], "errors": []}).encode(),
            )
        return 404, b""


def _attachments(fake: _FakeLangfuse, **kwargs) -> LangfuseAttachments:
    return LangfuseAttachments(
        api_base="https://langfuse.example",
        headers={"Authorization": "Basic dGVzdDp0ZXN0"},
        opener=fake,
        **kwargs,
    )


class TestLangfuseAttachments:
    def test_every_file_is_registered_uploaded_confirmed_and_the_manifest_sent(
        self, tmp_path: Path
    ) -> None:
        trial = write_trial(tmp_path / "trials" / "T" / "0", V3_FILES)
        fake = _FakeLangfuse()
        counts = _attachments(fake).attach(
            "a" * 32, trial, trace_timestamp=T0, metadata={"status": "completed"}
        )
        assert counts == AttachCounts(
            registered=11, uploaded=11, deduplicated=0, skipped=0, failed=0, manifests_sent=1
        )
        posts = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/api/public/media")]
        assert len(posts) == 11
        assert all(json.loads(c[3])["traceId"] == "a" * 32 for c in posts)
        assert all(json.loads(c[3])["field"] == "metadata" for c in posts)
        assert all(c[2]["Authorization"].startswith("Basic ") for c in posts)
        puts = [c for c in fake.calls if c[0] == "PUT"]
        assert len(puts) == 11 and all(c[2]["x-ms-blob-type"] == "BlockBlob" for c in puts)
        assert all("Authorization" not in c[2] for c in puts)  # the presigned URL carries the auth
        patches = [c for c in fake.calls if c[0] == "PATCH"]
        assert len(patches) == 11 and all(
            json.loads(c[3])["uploadHttpStatus"] == 200 for c in patches
        )
        ingest = [c for c in fake.calls if c[1].endswith("/api/public/ingestion")]
        assert len(ingest) == 1
        event = json.loads(ingest[0][3])["batch"][0]
        assert event["type"] == "trace-create"
        assert event["body"]["id"] == "a" * 32
        assert event["body"]["timestamp"] == T0.isoformat()
        manifest = event["body"]["metadata"]
        assert manifest["status"] == "completed"  # the trial's final status rides along
        assert manifest["attachments_schema"] == 2 and manifest["attachments_complete"] is True
        assert set(manifest["attachments"]) == set(V3_FILES)
        # the stored bytes are what the media object holds; env.yaml decodes to the file
        env = manifest["attachments"]["env.yaml"]
        assert hashlib.sha256(fake.blobs[env["media_id"]]).hexdigest() == env["stored_sha256"]
        assert gzip.decompress(fake.blobs[env["media_id"]]) == (trial / "env.yaml").read_bytes()
        assert (
            env["media"]
            == f"@@@langfuseMedia:type=application/gzip|id={env['media_id']}|source=bytes@@@"
        )

    def test_bytes_langfuse_already_holds_are_linked_not_uploaded(self, tmp_path: Path) -> None:
        first = write_trial(tmp_path / "a" / "trials" / "T" / "0", V1_FILES)
        second = write_trial(tmp_path / "b" / "trials" / "T" / "1", V1_FILES)
        fake = _FakeLangfuse()
        step = _attachments(fake)
        step.attach("a" * 32, first)
        counts = step.attach("b" * 32, second)
        assert counts.registered == 8 and counts.uploaded == 0 and counts.deduplicated == 8
        assert sum(1 for c in fake.calls if c[0] == "PUT") == 8

    def test_a_gate_hit_is_skipped_and_named_never_registered(self, tmp_path: Path) -> None:
        trial = write_trial(tmp_path / "trials" / "T" / "0", V1_FILES)
        (trial / "prompts.yaml").write_text(
            "system_prompt: use OPENROUTER_API_KEY=sk-or-v1-0123456789abcdef0123456789abcdef\n"
        )
        fake = _FakeLangfuse()
        counts = _attachments(fake, scan=SecretScan(["process-held-secret-value-123"])).attach(
            "a" * 32, trial
        )
        assert counts.registered == 7 and counts.skipped == 1 and counts.failed == 0
        manifest = json.loads(
            [c for c in fake.calls if c[1].endswith("/api/public/ingestion")][0][3]
        )["batch"][0]["body"]["metadata"]
        assert manifest["attachments_complete"] is False
        assert manifest["attachments_skipped"][0]["name"] == "prompts.yaml"
        assert "openrouter-key" in manifest["attachments_skipped"][0]["rule"]
        assert "0123456789abcdef" not in json.dumps(manifest)
        assert not any(b"sk-or-v1" in blob for blob in fake.blobs.values())

    def test_a_failing_put_is_counted_and_leaves_the_manifest_incomplete(
        self, tmp_path: Path
    ) -> None:
        trial = write_trial(tmp_path / "trials" / "T" / "0", V1_FILES)
        fake = _FakeLangfuse(fail_put=True)
        counts = _attachments(fake).attach("a" * 32, trial)
        assert counts.failed == 8 and counts.registered == 0 and counts.manifests_sent == 1
        manifest = json.loads(
            [c for c in fake.calls if c[1].endswith("/api/public/ingestion")][0][3]
        )["batch"][0]["body"]["metadata"]
        assert manifest["attachments"] == {} and manifest["attachments_complete"] is False

    def test_core_mode_and_s3_header(self, tmp_path: Path) -> None:
        trial = write_trial(tmp_path / "trials" / "T" / "0", V3_FILES)
        fake = _FakeLangfuse(azure=False)
        counts = _attachments(fake, mode=ATTACH_CORE).attach("a" * 32, trial)
        assert counts.registered == 5
        put = next(c for c in fake.calls if c[0] == "PUT")
        assert "x-amz-checksum-sha256" in put[2] and "x-ms-blob-type" not in put[2]
        digest = base64.b64decode(put[2]["x-amz-checksum-sha256"])
        assert digest == hashlib.sha256(put[3]).digest()

    def test_failed_files_are_named_and_the_budget_bounds_the_trial(self, tmp_path: Path) -> None:
        trial = write_trial(tmp_path / "trials" / "T" / "0", V1_FILES)
        fake = _FakeLangfuse(fail_put=True)
        counts = _attachments(fake).attach("a" * 32, trial)
        manifest = json.loads(
            [c for c in fake.calls if c[1].endswith("/api/public/ingestion")][0][3]
        )["batch"][0]["body"]["metadata"]
        # every failed file is named, so a download can say what is missing
        assert [s["name"] for s in manifest["attachments_skipped"]] == sorted(V1_FILES)
        assert all(s["rule"].startswith("upload-failed: ") for s in manifest["attachments_skipped"])
        assert counts.failed == 8 and counts.skipped == 0
        # a stalled receiver: the clock jumps past the budget on the first request, the rest of
        # the files are counted without another request, the manifest still goes out
        ticks = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0])
        slow = _FakeLangfuse()
        step = LangfuseAttachments(
            api_base="https://langfuse.example",
            opener=slow,
            budget_s=5.0,
            clock=lambda: next(ticks, 1000.0),
        )
        counts = step.attach("b" * 32, trial)
        posts = [c for c in slow.calls if c[0] == "POST" and c[1].endswith("/api/public/media")]
        assert len(posts) <= 2 and counts.failed >= 7
        assert counts.registered + counts.failed == 8

    def test_the_breaker_switches_the_step_off_after_three_dead_trials(
        self, tmp_path: Path
    ) -> None:
        def down(method, url, headers, body, timeout):
            raise OSError("connect: no route to host")

        step = LangfuseAttachments(api_base="https://langfuse.example", opener=down)
        for index in range(3):
            trial = write_trial(tmp_path / "trials" / "T" / str(index), V1_FILES)
            counts = step.attach(f"{index}" * 32, trial)
            assert counts.failed == 8 and counts.manifests_failed == 1
        assert step.tripped
        calls_before = len(step.__dict__)  # no new attribute, just the flag
        trial = write_trial(tmp_path / "trials" / "T" / "9", V1_FILES)
        counts = step.attach("9" * 32, trial)
        assert counts.failed == 8 and counts.manifests_failed == 1 and counts.registered == 0
        assert len(step.__dict__) == calls_before

    def test_a_malformed_presigned_url_never_reaches_a_log_line(
        self, tmp_path: Path, caplog
    ) -> None:
        trial = write_trial(tmp_path / "trials" / "T" / "0", ("task.yaml",))

        def odd(method, url, headers, body, timeout):
            if method == "POST" and url.endswith("/api/public/media"):
                return (
                    201,
                    json.dumps({"mediaId": "m1", "uploadUrl": "/upload/m1?sig=SECRETSIG"}).encode(),
                )
            return 207, b'{"errors": []}'

        step = LangfuseAttachments(api_base="https://langfuse.example", opener=odd)
        with caplog.at_level("WARNING"):
            counts = step.attach("a" * 32, trial)
        assert counts.failed == 1
        assert "SECRETSIG" not in caplog.text

    def test_urllib_opener_returns_status_and_body_for_success_and_http_errors(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from tolokaforge_langfuse.media import urllib_opener

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                status = 201 if self.path == "/ok" else 404
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            assert urllib_opener("POST", base + "/ok", {"X": "y"}, b"payload", 5) == (
                201,
                b"payload",
            )
            assert urllib_opener("POST", base + "/missing", {}, b"x", 5) == (404, b"x")
        finally:
            server.shutdown()
            server.server_close()

    def test_api_base_derives_from_the_otlp_endpoint(self) -> None:
        assert (
            api_base_from_endpoint("https://langfuse.example/api/public/otel/v1/traces")
            == "https://langfuse.example"
        )
        assert (
            api_base_from_endpoint("http://h:3000/lf/api/public/otel/v1/traces")
            == "http://h:3000/lf"
        )
        assert api_base_from_endpoint("https://h/") == "https://h"


class TestTrialEndStepExtras:
    """The parity amendment's additions to the receiver step: the environment on the manifest
    update, per-thread budgets, inline media on an observation, the event scan."""

    def test_the_manifest_update_carries_the_environment(self, tmp_path: Path) -> None:
        trial = write_trial(tmp_path / "trials" / "T" / "0", V3_FILES)
        fake = _FakeLangfuse()
        step = _attachments(fake, environment="staging")
        step.attach("a" * 32, trial)
        ingestion = [c for c in fake.calls if c[1].endswith("/api/public/ingestion")]
        (call,) = ingestion
        body = json.loads(call[3])["batch"][0]["body"]
        assert body["environment"] == "staging" and body["metadata"]["attachments_schema"] == 2
        assert _attachments(_FakeLangfuse())._environment is None

    def test_budgets_are_per_thread(self, tmp_path: Path) -> None:
        import threading

        step = _attachments(_FakeLangfuse(), budget_s=100.0)
        seen: dict[str, float | None] = {}
        gate = threading.Event()

        def worker(name: str) -> None:
            with step.budget():
                seen[f"{name}-inside"] = step._deadline
                gate.wait(2.0)
            seen[f"{name}-after"] = step._deadline

        first = threading.Thread(target=worker, args=("a",))
        first.start()
        # the main thread opens and closes its own budget while the worker holds one
        with step.budget():
            assert step._deadline is not None
        assert step._deadline is None
        gate.set()
        first.join()
        assert seen["a-inside"] is not None and seen["a-after"] is None

    def test_inline_media_registers_on_the_observation_and_scans_the_bytes(
        self, tmp_path: Path
    ) -> None:
        fake = _FakeLangfuse()
        step = _attachments(fake)
        token = step.register_media("a" * 32, "b" * 16, "output", "image/png", b"\x89PNG fake")
        assert token == "@@@langfuseMedia:type=image/png|id=m1|source=bytes@@@"
        registration = json.loads(next(c for c in fake.calls if c[1].endswith("/media"))[3])
        assert registration["observationId"] == "b" * 16 and registration["field"] == "output"
        # bytes that carry a key shape never leave
        assert (
            step.register_media(
                "a" * 32, "b" * 16, "output", "text/plain", b"sk-or-v1-" + b"a" * 40
            )
            is None
        )

    def test_scan_events_serialises_foreign_values(self) -> None:
        import datetime

        step = _attachments(_FakeLangfuse())
        assert step.scan_events([{"body": {"when": datetime.date(2026, 9, 17)}}]) == []
        assert step.scan_events([{"body": {"k": "sk-or-v1-" + "a" * 40}}])
