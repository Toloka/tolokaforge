"""Integration tests for the reset-recipe dispatchers.

Each module exercises one :data:`~tolokaforge.core.models.SeedKind`
dispatcher against a real container: seed the service, mutate it,
apply the recipe, assert the service is back to the seeded baseline.
Marked ``integration`` — requires Docker.
"""
