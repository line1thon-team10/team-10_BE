"""study persistence for the ERD Core domain models. Pass ownership predicates on user-facing access."""
from app.db import schema
from app.repositories.base import Repository


class StudyBlocksRepository(Repository):
    table = schema.study_blocks


class StudyTasksRepository(Repository):
    table = schema.study_tasks


class StudyTaskRangesRepository(Repository):
    table = schema.study_task_ranges


class StudySessionsRepository(Repository):
    table = schema.study_sessions


class StudySessionSegmentsRepository(Repository):
    table = schema.study_session_segments


class StudyRecordsRepository(Repository):
    table = schema.study_records


class StudyRecordRangesRepository(Repository):
    table = schema.study_record_ranges


class SpeedCalibrationsRepository(Repository):
    table = schema.speed_calibrations


class MasteryHistoryRepository(Repository):
    table = schema.mastery_history


class LearningPassesRepository(Repository):
    table = schema.learning_passes
