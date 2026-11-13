"""user persistence for the ERD Core domain models. Pass ownership predicates on user-facing access."""
from app.db import schema
from app.repositories.base import Repository


class UsersRepository(Repository):
    table = schema.users


class OnboardingMessagesRepository(Repository):
    table = schema.onboarding_messages


class UserNoticesRepository(Repository):
    table = schema.user_notices
