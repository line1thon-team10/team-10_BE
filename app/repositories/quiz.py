"""quiz persistence for the ERD Core domain models. Pass ownership predicates on user-facing access."""
from app.db import schema
from app.repositories.base import Repository


class QuestionsRepository(Repository):
    table = schema.questions


class QuestionSourcesRepository(Repository):
    table = schema.question_sources


class QuizSessionsRepository(Repository):
    table = schema.quiz_sessions


class QuizScopesRepository(Repository):
    table = schema.quiz_scopes


class QuizItemsRepository(Repository):
    table = schema.quiz_items


class QuizAttemptsRepository(Repository):
    table = schema.quiz_attempts


class WeakQuestionsRepository(Repository):
    table = schema.weak_questions
