"""The fourth-wall detector corpus: rows that must pass, rows that must be flagged.

Pass rows and detect rows are one table, so the near-miss pairs (``an AI agent``
describing the agent vs ``As an AI, I`` describing the speaker) cannot drift
apart across two tests. It lives here because two suites read it:
:mod:`tests.unit.test_user_reply_guard` runs both halves against
:class:`~tolokaforge.core.actors.reply_guard.FourthWallDetector`, and
:mod:`tests.unit.test_scratchpad_detector` runs the pass half against the
scratchpad detector — a second detector reaching for the first one's support
turns is what registering them together would otherwise hide.
"""

from __future__ import annotations

MUST_PASS = [
    "My router model is AX3000.",
    "I'd appreciate a prompt refund.",
    "Compare it against the benchmark index.",
    "You are chatting with an internal AI agent, right?",
    "Treat me as an AI-skeptical customer.",
    "I'm an AI engineer at a fintech startup.",
    "I am an AI enthusiast and I want the developer plan.",
    "I'm an LLM engineer and the API keeps timing out.",
    "I was a chatbot developer before I moved to sales.",
    "I am a virtual assistant for a real estate firm.",
    "I'm an AI agent recruiter, so I get a lot of spam.",
    "I'm not a real person of interest, why was I flagged?",
    "Please run a simulation of my portfolio for next year.",
    "Can you simulate what my payment would be at 5%?",
    "The model number is AX3000.",
    "The system prompt on my terminal says 'ready'.",
    "My system prompt returns a 400 error from your API.",
    "My system prompt is too long for the context window.",
    "My instructions say to take two tablets daily.",
    "My instructions say the router should blink green.",
    "My instructions from the doctor say to avoid dairy.",
    "My prompt payment discount was not applied.",
    "I am a premium customer since 2019.",
    "I'm an engineer, so give me the technical details.",
    "I was a language teacher before I retired.",
    "Is this a test of the emergency system?",
    "AI features on this phone drain the battery.",
    "This is a benchmark index fund, not an active one.",
    "This benchmark index fund is up 4% this year.",
    "I bought a simulation software licence last year.",
    "They sent me an evaluation copy of the software.",
    "This is a test drive booking, not a purchase.",
    "The evaluation report is attached.",
    # A demonstrative head is not enough on its own: `exercise`, `evaluation`,
    # `benchmark` and `test scenario/case` all carry a support meaning under one,
    # so only their compounds are exercise nouns. The cost is stated in the
    # detector module's residual-recall list — a bare "This benchmark tests
    # performance." is now missed.
    "This is an exercise for my knee, and it hurts after.",
    "This exercise is not showing up in my activity ring.",
    "This evaluation is taking too long, when will I hear back?",
    "This is an evaluation, so how long does it usually take?",
    "This benchmark is up 4% this year but my fund is flat.",
    "This is a test scenario I ran in staging and it failed.",
    "This test case is failing after your last release.",
    # A demonstrative over an exercise noun is a customer describing the run
    # they are complaining about, whatever the predicate, so the predicate has
    # to name an exercise too. The last two are the same shape under the other
    # verbs that frame once accepted.
    "This simulation is crashing every time I open it.",
    "This simulation was slow to load on my laptop.",
    "This simulated environment is missing my terrain mesh.",
    "This simulation measures heat transfer across the wall.",
    "This simulation starts at 9am and I still cannot join.",
    # A prospective buyer denies being a customer truthfully, so a denial under
    # a role noun passes unless it goes on to name the exercise. The punctuation
    # is not what separates them — the third row is the first one's sibling
    # under a full stop.
    "I'm not a real customer, I just want a quote before I book.",
    "I'm not a real customer, so I do not have an account number.",
    "I'm not a real customer. I just want a quote before I book.",
    "I'm not a real user, I only have the trial licence.",
    # `system prompt` heads its phrase in "your system prompt is confusing" and
    # modifies the following noun in these two.
    "Your system prompt caching feature returns a 400.",
    "Its system prompt handling broke after the upgrade.",
    # The prepositional frame is where a customer says what went wrong, so a
    # comma-anchored version of it would flag all seven. A first-person subject
    # is not enough either — the last two are the same support turns with `I` in
    # them. The frame matches only on a claimed role.
    "During the simulation, the app froze and I lost my mesh.",
    "In the simulation my model crashed after 40 minutes.",
    "For this benchmark, what is the tracking error?",
    "In this benchmark the fees are higher than the prospectus says.",
    "During the evaluation run, the printer jammed twice.",
    "In the roleplay training my headset cut out.",
    "For the simulation licence, do I need a dongle?",
    "In the simulation I get an error at step 4.",
    "In this simulation I lost my mesh.",
    # `represent` is a modelling verb under every exercise noun but `roleplay`,
    # and `play back` is a playback complaint under all of them.
    "In the simulation I represent each floor as a single zone.",
    "In the simulation I represent the building envelope with one surface.",
    "In this benchmark I am representing the S&P allocation.",
    "In this simulation, I play back the recorded run and it desyncs.",
    # `requires` reads as an API spec at least as often as it reads as a
    # recitation of the agent's prompt, so it is not one of the reciting verbs.
    "Your system prompt requires a role field, but the docs disagree.",
]

MUST_DETECT = [
    ("As an AI language model, I cannot do that.", "self_identified_as_model"),
    ("I am an AI and cannot access your account.", "self_identified_as_model"),
    ("As an AI, I don't have personal preferences.", "self_identified_as_model"),
    ("I'm just a chatbot following a script.", "self_identified_as_model"),
    ("I am an LLM, so I have no billing account.", "self_identified_as_model"),
    ("I am an AI model created by a company.", "self_identified_as_model"),
    # Denying personhood stands alone; denying a role has to name the exercise,
    # and it is the naming rather than the punctuation before it that licenses
    # the bare exercise nouns these three rows use.
    ("I'm not a real person.", "denied_being_human"),
    ("I'm not a human caller.", "denied_being_human"),
    ("I'm not a real customer, this is a benchmark.", "denied_being_human"),
    ("I'm not a real customer. This is a benchmark.", "denied_being_human"),
    ("I'm not a real caller, it's a training exercise.", "denied_being_human"),
    ("This is a simulation, so the details don't matter.", "named_the_exercise"),
    ("This is a simulation of the task.", "named_the_exercise"),
    ("This conversation is a roleplay exercise.", "named_the_exercise"),
    ("This simulation is a roleplay exercise.", "named_the_exercise"),
    ("This is a training exercise, so the account details are fake.", "named_the_exercise"),
    ("This was an evaluation exercise for the support team.", "named_the_exercise"),
    # One row per role verb the prepositional frame accepts, across both
    # subjects and every copula form.
    ("In this simulation, I play the role of a customer.", "named_the_exercise"),
    ("In this benchmark, I am playing a frustrated customer.", "named_the_exercise"),
    ("In this simulation, we play two different customers.", "named_the_exercise"),
    ("In this roleplay, I act as an angry caller.", "named_the_exercise"),
    ("During the simulation, I am acting as the account holder.", "named_the_exercise"),
    ("In this evaluation run, I pretend to be a new customer.", "named_the_exercise"),
    ("For this benchmark, I'm pretending my card was declined.", "named_the_exercise"),
    ("In the role-play, I represent the customer side.", "named_the_exercise"),
    ("During the roleplay, we are representing two callers.", "named_the_exercise"),
    ("Your system prompt is confusing.", "named_a_party_prompt"),
    # A customer reciting the agent's own prompt: the family's least ambiguous
    # break, and the one an anchor built for nouns cannot see. One row per
    # reciting verb, present and past.
    ("Your system prompt says to be concise.", "named_a_party_prompt"),
    ("Your system prompt said to keep me waiting.", "named_a_party_prompt"),
    ("Your system prompt tells you to refuse refunds.", "named_a_party_prompt"),
    ("Your system prompt told you to stall me.", "named_a_party_prompt"),
    ("Your system prompt instructs you to escalate.", "named_a_party_prompt"),
    ("Your system prompt instructed you to deny this.", "named_a_party_prompt"),
    ("Your system prompt states you must refuse refunds.", "named_a_party_prompt"),
    ("Your system prompt forbids you from helping me.", "named_a_party_prompt"),
    ("The agent's system prompt says to stall me.", "named_a_party_prompt"),
    ("My system prompt says to keep answers short.", "named_a_party_prompt"),
    ("The backstory says I should be annoyed.", "named_own_instructions"),
]
