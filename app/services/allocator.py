"""Pure allocation-v1 solver: exact feasibility first, then daily objectives.

No DB, clock, AI, or endpoint calls. A resource stop raises SearchLimit; only an
exhausted exact search can report a combinatorial plan as infeasible.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
import heapq
import json
from uuid import NAMESPACE_URL, uuid5

from app.domain.ranges import intersect, normalize, page_count, subtract
from app.domain.scheduling import (
    DAY_END, DAY_START, MAX_TASK, MIN_TASK, MULTIPLIERS, POLICY_VERSION, AllocatedPart,
    AllocationInput, AllocationReason, AllocationResult, Placement, ScopePart, SearchLimit, Slot,
    Unplaced, allocation_score, free_slots, group_targets, mastery_ratio, reservations, take_pages,
)


@dataclass(frozen=True)
class _Pool:
    requirement_id: str
    subject_id: str
    kind: str
    unit_id: str
    spans: tuple
    release: date
    baseline: Fraction
    multiplier: Fraction

    @property
    def cost(self):
        return self.baseline * self.multiplier

    @property
    def pages(self):
        return page_count(self.spans)


@dataclass(frozen=True)
class _Task:
    day: date
    start: Fraction
    end: Fraction
    counts: tuple[int, ...]


class _Budget:
    def __init__(self, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError("max_search_nodes must be a positive integer")
        self.limit, self.explored = limit, 0

    def tick(self, count=1):
        self.explored += count
        if self.explored > self.limit:
            raise SearchLimit(self.limit, self.explored)


class _Allocator:
    def __init__(self, request: AllocationInput, budget: _Budget):
        self.request, self.budget = request, budget
        self.subjects = {subject.id: subject for subject in request.subjects}
        self.units = {unit.id: unit for unit in request.units}
        if len(self.subjects) != len(request.subjects) or len(self.units) != len(request.units):
            raise ValueError("Subject and unit identifiers must be unique")
        if len({task.id for task in request.fixed}) != len(request.fixed):
            raise ValueError("Fixed task identifiers must be unique")
        if len({work.id for work in request.requirements}) != len(request.requirements):
            raise ValueError("Obligation identifiers must be unique")
        for unit in request.units:
            if unit.subject_id not in self.subjects:
                raise ValueError("A unit must belong to an input subject")
        # Current outlines reject cross-unit overlap. Concept overlap inside a
        # unit has already become a union, so its speed is counted only once.
        material_owner, covered = {}, defaultdict(tuple)
        for unit in sorted(request.units, key=lambda item: item.id):
            owner = material_owner.setdefault(unit.material_id, unit.subject_id)
            if owner != unit.subject_id or intersect(covered[unit.material_id], unit.ranges):
                raise ValueError("Current material pages need an unambiguous subject/unit owner")
            covered[unit.material_id] = normalize((*covered[unit.material_id], *unit.ranges))
        for task in request.fixed:
            if task.subject_id not in self.subjects:
                raise ValueError("A fixed task must belong to an input subject")
            for part in task.ranges:
                unit = self.units.get(part.unit_id)
                if unit and (unit.subject_id != task.subject_id or unit.material_id != part.span.material_id):
                    raise ValueError("A fixed range cannot refer to another subject/material")
        self.pools = self._pools()
        self.initial = tuple(pool.pages for pool in self.pools)
        self.mastery = {subject: mastery_ratio(subject, request.units) for subject in self.subjects}
        self.fit_cache = {}
        self.task_cache = {}

    def _key(self, pool):
        unit, subject = self.units[pool.unit_id], self.subjects[pool.subject_id]
        return (subject.exam_date or date.max, subject.id, unit.material_id, unit.sort_order,
                pool.spans[0].start, pool.requirement_id, pool.kind, unit.id)

    def _pools(self):
        pools = []
        for unit in self.request.units:
            done = [part.span for task in self.request.fixed if task.subject_id == unit.subject_id and task.kind == "study"
                    for part in task.ranges]
            remaining = subtract(unit.ranges, done)
            if remaining:
                subject = self.subjects[unit.subject_id]
                pools.append(_Pool(f"study:{unit.id}", subject.id, "study", unit.id, remaining,
                                   self.request.start_date, subject.minutes_per_page, MULTIPLIERS[unit.difficulty]))
        for work in self.request.requirements:
            if work.subject_id not in self.subjects:
                raise ValueError("An obligation must belong to an input subject")
            grouped = defaultdict(list)
            for part in work.parts:
                unit = self.units.get(part.unit_id)
                if unit is None or unit.subject_id != work.subject_id or unit.material_id != part.span.material_id:
                    raise ValueError("An obligation range must match its subject, unit and material")
                grouped[part.unit_id].append(part.span)
            occupied = ()
            for unit_id in sorted(grouped):
                spans = normalize(grouped[unit_id])
                if intersect(occupied, spans):
                    raise ValueError("An obligation page cannot have conflicting unit ownership")
                occupied = normalize((*occupied, *spans))
                fulfilled = [part.span for task in self.request.fixed
                             if task.subject_id == work.subject_id and task.kind == work.kind
                             for part in task.ranges if part.requirement_id == work.id]
                remaining = subtract(spans, fulfilled)
                if remaining:
                    unit, subject = self.units[unit_id], self.subjects[work.subject_id]
                    pools.append(_Pool(work.id, work.subject_id, work.kind, unit_id, remaining,
                                       max(work.not_before or self.request.start_date, self.request.start_date),
                                       subject.minutes_per_page, MULTIPLIERS[unit.difficulty]))
        return tuple(sorted(pools, key=self._key))

    def _duration(self, counts):
        return sum((count * pool.cost for count, pool in zip(counts, self.pools)), Fraction())

    def _scores(self, remaining, day):
        pages = defaultdict(int)
        for count, pool in zip(remaining, self.pools):
            if pool.kind == "study":
                pages[pool.subject_id] += count
        return {key: allocation_score(subject, day, pages[key], self.mastery[key])
                for key, subject in self.subjects.items()}

    def _eligible(self, index, day):
        pool = self.pools[index]
        return pool.release <= day < self.subjects[pool.subject_id].exam_date

    def _task_choices(self, remaining, day, capacity):
        capacity = min(capacity, MAX_TASK)
        key = remaining, day, capacity
        if key in self.task_cache:
            return self.task_cache[key]
        groups = defaultdict(list)
        for i, count in enumerate(remaining):
            if count and self._eligible(i, day):
                pool = self.pools[i]
                groups[(pool.subject_id, pool.kind)].append(i)
        scores, choices = self._scores(remaining, day), []
        for (subject_id, _), indices in groups.items():
            counts = [0] * len(remaining)
            def collect(position, duration):
                self.budget.tick()
                if position == len(indices):
                    if MIN_TASK <= duration <= capacity:
                        vector = tuple(counts)
                        choices.append(((-scores[subject_id], self._key(self.pools[indices[0]]),
                                         -duration, tuple(-count for count in vector)), vector, duration))
                    return
                i = indices[position]
                maximum = min(remaining[i], (capacity - duration) // self.pools[i].cost)
                self.budget.tick(maximum + 1)
                for count in range(maximum, -1, -1):
                    counts[i] = count
                    collect(position + 1, duration + count * self.pools[i].cost)
                counts[i] = 0
            collect(0, Fraction())
        result = tuple((counts, duration) for _, counts, duration in sorted(choices))
        self.task_cache[key] = result
        return result

    def _fit(self, remaining, slots):
        """Complete left-justified interval packing; page pools are interchangeable.

        Tasks may combine unit/material fragments of the same subject and kind.
        Every integer count vector in 15..60 is considered, including a smaller
        first task that saves a short tail or leaves room for an urgent subject.
        """
        slots = tuple(slot for slot in slots if slot.minutes >= MIN_TASK)
        cache_key = remaining, slots
        if cache_key in self.fit_cache:
            return self.fit_cache[cache_key]
        failed = set()

        def search(counts, slot_index, start):
            self.budget.tick()
            if not any(counts):
                return ()
            if slot_index == len(slots):
                return None
            slot = slots[slot_index]
            if slot.end - start < MIN_TASK:
                return search(counts, slot_index + 1, slots[slot_index + 1].start if slot_index + 1 < len(slots) else Fraction())
            key = counts, slot_index, start
            if key in failed:
                return None
            available = (Slot(slot.day, start, slot.end), *slots[slot_index + 1:])
            if self._duration(counts) > sum((item.minutes for item in available), Fraction()):
                failed.add(key)
                return None
            grouped = defaultdict(Fraction)
            for count, pool in zip(counts, self.pools):
                grouped[(pool.subject_id, pool.kind)] += count * pool.cost
            if any(0 < duration < MIN_TASK for duration in grouped.values()):
                failed.add(key)
                return None
            for deadline in {self.subjects[pool.subject_id].exam_date for i, pool in enumerate(self.pools) if counts[i]}:
                required = sum((count * pool.cost for count, pool in zip(counts, self.pools)
                                if self.subjects[pool.subject_id].exam_date <= deadline), Fraction())
                if required > sum((item.minutes for item in available if item.day < deadline), Fraction()):
                    failed.add(key)
                    return None
            for vector, duration in self._task_choices(counts, slot.day, slot.end - start):
                rest = tuple(count - used for count, used in zip(counts, vector))
                tail = search(rest, slot_index, start + duration)
                if tail is not None:
                    return (_Task(slot.day, start, start + duration, vector), *tail)
            tail = search(counts, slot_index + 1, slots[slot_index + 1].start if slot_index + 1 < len(slots) else Fraction())
            if tail is None:
                failed.add(key)
            return tail

        try:
            result = search(remaining, 0, slots[0].start if slots else Fraction())
        except RecursionError as exc:
            # A stack/resource boundary is never a proof of infeasibility.
            raise SearchLimit(self.budget.limit, self.budget.explored) from exc
        self.fit_cache[cache_key] = result
        return result

    def _subject_targets(self, amount, caps, scores):
        targets = {key: Fraction() for key in caps}
        pending = set(caps)
        while amount > 0 and pending:
            positives = {key for key in pending if scores[key] > 0}
            if not positives:
                for key in sorted(pending, key=lambda key: (self.subjects[key].exam_date, key)):
                    used = min(amount, caps[key] - targets[key])
                    targets[key] += used
                    amount -= used
                break
            total = sum((scores[key] for key in positives), Fraction())
            shares = {key: amount * scores[key] / total for key in positives}
            capped = {key for key in positives if shares[key] >= caps[key] - targets[key]}
            if not capped:
                for key, share in shares.items():
                    targets[key] += share
                break
            for key in capped:
                used = caps[key] - targets[key]
                targets[key] += used
                amount -= used
                pending.remove(key)
        return targets

    def _day_targets(self, remaining, day, focus):
        fixed = defaultdict(Fraction)
        for task in self.request.fixed:
            if task.day == day:
                fixed[task.subject_id] += task.minutes
        caps = dict(fixed)
        for i, (count, pool) in enumerate(zip(remaining, self.pools)):
            if self._eligible(i, day):
                caps[pool.subject_id] = caps.get(pool.subject_id, Fraction()) + count * pool.cost
        capacity = sum((slot.minutes for slot in free_slots(day, self.request.busy)), Fraction())
        scores = self._scores(remaining, day)
        if focus is None:
            target = self._subject_targets(min(capacity, sum(caps.values(), Fraction())), caps, scores)
            return fixed, target, None
        focus = frozenset(focus)
        first = {key: value for key, value in caps.items() if key in focus}
        second = {key: value for key, value in caps.items() if key not in focus}
        group_target = group_targets(capacity, sum(first.values(), Fraction()), sum(second.values(), Fraction()))
        target = {**self._subject_targets(group_target[0], first, scores),
                  **self._subject_targets(group_target[1], second, scores)}
        return fixed, target, (focus, group_target)

    def _day(self, remaining, day, slots, rest_slots, focus):
        """Enumerate daily page-count allocations in exact policy objective order.

        A branch-and-bound heap supplies an admissible lower bound for remaining
        count choices. Each leaf must pack into real slots AND permit completion
        of all other days before it can win the daily objective.
        """
        fixed, targets, group = self._day_targets(remaining, day, focus)
        indices = tuple(i for i, count in enumerate(remaining) if count and self._eligible(i, day))
        capacity = sum((slot.minutes for slot in slots), Fraction())

        def bounds(partial):
            minimum, maximum = dict(fixed), dict(fixed)
            vector = [0] * len(remaining)
            for position, i in enumerate(indices):
                pool = self.pools[i]
                low = partial[position] if position < len(partial) else 0
                high = partial[position] if position < len(partial) else remaining[i]
                minimum[pool.subject_id] = minimum.get(pool.subject_id, Fraction()) + low * pool.cost
                maximum[pool.subject_id] = maximum.get(pool.subject_id, Fraction()) + high * pool.cost
                vector[i] = -high
            def distance(target, low, high):
                return max(low - target, target - high, Fraction())
            secondary = sum((distance(target, minimum.get(key, Fraction()), maximum.get(key, Fraction()))
                             for key, target in targets.items()), Fraction())
            primary = Fraction()
            if group:
                selected, amounts = group
                for included, target in ((True, amounts[0]), (False, amounts[1])):
                    low = sum((value for key, value in minimum.items() if (key in selected) == included), Fraction())
                    high = sum((value for key, value in maximum.items() if (key in selected) == included), Fraction())
                    primary += distance(target, low, high)
            return primary, secondary, tuple(vector)

        def candidate(partial):
            counts = [0] * len(remaining)
            for i, count in zip(indices, partial):
                counts[i] = count
            counts = tuple(counts)
            layout = self._fit(counts, slots)
            if layout is None:
                return None
            rest = tuple(count - used for count, used in zip(remaining, counts))
            if self._fit(rest, rest_slots) is None:
                return None
            return layout, rest, bounds(partial)[0]

        # All eligible work is an exact optimum when both target deviations are
        # zero. This avoids enumerating the powerset of a small full-study day.
        full = tuple(remaining[i] for i in indices)
        if bounds(full)[:2] == (0, 0) and sum((remaining[i] * self.pools[i].cost for i in indices), Fraction()) <= capacity:
            result = candidate(full)
            if result is not None:
                return result
        serial = 0
        heap = [(bounds(()), serial, (), Fraction())]
        while heap:
            self.budget.tick()
            _, _, partial, used = heapq.heappop(heap)
            if len(partial) == len(indices):
                result = candidate(partial)
                if result is not None:
                    return result
                continue
            i = indices[len(partial)]
            maximum = min(remaining[i], (capacity - used) // self.pools[i].cost)
            self.budget.tick(maximum + 1)
            for count in range(maximum, -1, -1):
                child = (*partial, count)
                serial += 1
                heapq.heappush(heap, (bounds(child), serial, child, used + count * self.pools[i].cost))
        # The initial global oracle and every preceding accepted day guaranteed
        # a completion, so reaching this point would be an implementation bug.
        raise RuntimeError("A proven feasible remainder lost all daily allocations")

    def _unplaced(self):
        grouped = defaultdict(list)
        for pool in self.pools:
            grouped[(pool.requirement_id, pool.subject_id, pool.kind)].extend(ScopePart(pool.unit_id, span) for span in pool.spans)
        return tuple(Unplaced(*key, tuple(parts)) for key, parts in sorted(grouped.items()))

    def _result(self, feasible, tasks=(), reasons=()):
        fixed = tuple(sorted(self.request.fixed, key=lambda task: (task.day, task.start, task.id)))
        if not feasible:
            return AllocationResult(False, (), fixed, self._unplaced(), tuple(reasons))
        ranges = [pool.spans for pool in self.pools]
        placements = []
        for task in sorted(tasks, key=lambda item: (item.day, item.start, tuple(-count for count in item.counts))):
            parts = []
            owners = set()
            for i, count in enumerate(task.counts):
                if not count:
                    continue
                pool = self.pools[i]
                owners.add((pool.subject_id, pool.kind))
                taken, ranges[i] = take_pages(ranges[i], count)
                parts.extend(AllocatedPart(pool.requirement_id, pool.unit_id, span, pool.baseline, pool.multiplier) for span in taken)
            if len(owners) != 1 or not MIN_TASK <= task.end - task.start <= MAX_TASK:
                raise RuntimeError("Invalid generated task")
            subject_id, kind = owners.pop()
            identity = json.dumps([POLICY_VERSION, subject_id, kind, task.day.isoformat(), str(task.start), str(task.end),
                                   [[part.requirement_id, part.unit_id, part.span.material_id, part.span.start, part.span.end,
                                     str(part.baseline_minutes_per_page), str(part.difficulty_multiplier)] for part in parts]],
                                  ensure_ascii=False, separators=(",", ":"))
            placements.append(Placement(str(uuid5(NAMESPACE_URL, identity)), subject_id, kind, task.day, task.start, task.end, tuple(parts)))
        if any(ranges):
            raise RuntimeError("A successful plan must preserve every required page")
        return AllocationResult(True, tuple(placements), fixed, (), tuple(reasons))

    def run(self):
        if any(not subject.exam_confirmed or not subject.goal_confirmed or subject.exam_date is None or subject.target_score is None
               for subject in self.subjects.values()):
            return self._result(False, reasons=[AllocationReason("UNCONFIRMED_INPUT", "시험일과 목표 점수를 먼저 확인해 주세요")])
        current_fixed = sorted((task for task in self.request.fixed if task.day >= self.request.start_date),
                               key=lambda task: (task.day, task.start, task.id))
        for i, task in enumerate(current_fixed):
            if task.day >= self.subjects[task.subject_id].exam_date or task.start < DAY_START or task.end > DAY_END:
                return self._result(False, reasons=[AllocationReason("FIXED_CONFLICT", "고정 태스크가 시험일 또는 공부 가능 시간과 충돌해요", task.day)])
            others = [item for item in (*self.request.busy, *current_fixed[:i]) if item.day == task.day]
            if any(max(task.start, item.start) < min(task.end, item.end) for item in others):
                return self._result(False, reasons=[AllocationReason("FIXED_CONFLICT", "시작·완료 태스크를 보존하면 고정 일정이 겹쳐요", task.day)])
        if not any(self.initial):
            return self._result(True)
        final_exam = max(subject.exam_date for subject in self.subjects.values())
        days, day = {}, self.request.start_date
        while day < final_exam:
            self.budget.tick()
            days[day] = free_slots(day, self.request.busy, self.request.fixed)
            day += timedelta(days=1)
        all_slots = tuple(slot for slots in days.values() for slot in slots)
        if self._fit(self.initial, all_slots) is None:
            return self._result(False, reasons=[AllocationReason(
                "PLAN_INFEASIBLE", "시험일 전 연속 가용 시간에 모든 범위를 15~60분 정수 페이지 태스크로 배치할 수 없어요")])
        reserved = reservations(self.request.subjects, self.request.start_date)
        claimed = {day for day, _ in reserved}
        order = (*reserved, *((day, None) for day in days if day not in claimed))
        remaining, tasks, reasons = self.initial, [], []
        available_days = dict(days)
        for day, focus in order:
            slots = available_days.pop(day)
            rest_slots = tuple(slot for _, others in sorted(available_days.items()) for slot in others)
            layout, remaining, deviation = self._day(remaining, day, slots, rest_slots, focus)
            tasks.extend(layout)
            if focus is not None and deviation:
                fixed_here = any(task.day == day for task in self.request.fixed)
                detail = "고정 태스크와 정수 페이지·연속 구간 제약" if fixed_here else "정수 페이지·15~60분·연속 구간 및 다른 필수 범위 제약"
                reasons.append(AllocationReason("RESERVATION_DEVIATION", f"{detail}으로 직전일 목표 배분과 {deviation}분 차이가 있어요", day, deviation))
        if any(remaining):
            raise RuntimeError("Feasible result still contains unallocated obligations")
        return self._result(True, tasks, reasons)


def allocate(request: AllocationInput, *, max_search_nodes: int = 250_000) -> AllocationResult:
    """Compute a complete candidate or a proven infeasible result.

    SearchLimit is a distinct internal outcome requiring more resources; callers
    must not translate it into PLAN_INFEASIBLE or approve an unfinished result.
    """
    if not isinstance(request, AllocationInput):
        raise ValueError("AllocationInput is required")
    return _Allocator(request, _Budget(max_search_nodes)).run()
