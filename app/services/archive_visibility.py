"""Hide cancelled future placements without changing archived learning facts."""
from datetime import timezone
from zoneinfo import ZoneInfo

from app.core.clock import today_for
from app.services.subject_archive_state import archived_dates


class ArchiveVisibility:
    def __init__(self, conn, user, subjects):
        self.subjects = {subject['id']: subject for subject in subjects}
        self.cutoffs = archived_dates(conn, user, subjects)
        self.today = today_for(user)
        self.zone = ZoneInfo(user.get('timezone') or 'Asia/Seoul')

    def date(self, subject_id, day):
        cutoff = self.cutoffs.get(subject_id)
        return cutoff is None or day < cutoff

    def task(self, task):
        row = task.row
        cutoff = self.cutoffs.get(row['subject_id'])
        if cutoff is None:
            return True
        day = row['scheduled_date']
        if day < cutoff:
            return True
        # Recording facts survive archival. Future completion is never inferred.
        if day <= self.today and task.record and task.record['result'] in ('completed', 'partial'):
            return True
        if day > cutoff:
            return False
        archived_at = self.subjects[row['subject_id']].get('archived_at')
        starts_at = row.get('starts_at')
        if archived_at is None or starts_at is None:
            return False
        def local(value):
            return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(self.zone)
        # The persistent cutoff date supplies the virtual date; the real archive
        # timestamp supplies the time of day for same-day scheduled placements.
        archive_local = local(archived_at)
        cutoff_at = archive_local.replace(year=cutoff.year, month=cutoff.month, day=cutoff.day)
        return local(starts_at).astimezone(timezone.utc) < cutoff_at.astimezone(timezone.utc)
