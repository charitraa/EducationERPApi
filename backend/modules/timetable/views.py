import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from django_filters import rest_framework as filters
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.audit.services import log_create, log_delete, log_update, snapshot
from core.common.exceptions import ConflictError
from core.common.mixins import CampusScopedViewSet
from core.permissions.selectors import campus_ids_with_permission
from modules.academics.models import CurriculumSubject
from modules.academics.selectors import chosen_elective_ids, students_taking
from modules.academics.serializers import TeachingAssignmentSerializer
from modules.parents.selectors import links_for_parent, parent_for_user
from modules.staff.selectors import staff_member_for_user
from modules.students.selectors import get_current_enrollment, student_for_user

from .generator import plan_timetable
from .models import BellSchedule, LessonChange, Period, TimetableEntry
from .serializers import (
    BellScheduleSerializer,
    GenerateSerializer,
    HandOverSerializer,
    LessonChangeSerializer,
    LessonSerializer,
    PeriodSerializer,
    RetimeSerializer,
    TimetableEntrySerializer,
)
from .services import (
    ENTRY_RELATED,
    entries_on,
    find_date_clashes,
    hand_over,
    has_run_before,
    lessons_on,
    lock,
    lock_and_check,
    continue_across_retimes,
    raise_if_clashes,
    retime_schedule,
    running_from,
    span,
    supersede,
)

VIEW = "timetable.view"
MANAGE = "timetable.manage"


def _perms(**extra) -> dict:
    return {
        "list": [VIEW], "retrieve": [VIEW],
        "create": [MANAGE], "update": [MANAGE], "partial_update": [MANAGE], "destroy": [MANAGE],
        **extra,
    }


def _schema(noun: str):
    tag = "timetable"
    return extend_schema_view(
        list=extend_schema(tags=[tag], summary=f"List {noun}s"),
        retrieve=extend_schema(tags=[tag], summary=f"Retrieve a {noun}"),
        create=extend_schema(tags=[tag], summary=f"Create a {noun}"),
        update=extend_schema(tags=[tag], summary=f"Replace a {noun}"),
        partial_update=extend_schema(tags=[tag], summary=f"Update a {noun}"),
        destroy=extend_schema(tags=[tag], summary=f"Delete a {noun}"),
    )


def _query_date(request, required=True, name="date"):
    raw = request.query_params.get(name)
    if raw is None and not required:
        return None
    value = parse_date(raw or "")
    if value is None:
        raise ValidationError({name: "Give a date as YYYY-MM-DD."})
    return value


def _query_id(request, name):
    raw = request.query_params.get(name)
    if raw is None:
        return None
    if not raw.isdigit():
        raise ValidationError({name: "Must be an id."})
    return int(raw)


class TimetableViewSet(CampusScopedViewSet):
    audit_module = "timetable"
    required_permissions = _perms()

    def visible_campus_ids(self):
        return campus_ids_with_permission(self.request.user, VIEW)


@_schema("bell schedule")
class BellScheduleViewSet(TimetableViewSet):
    queryset = BellSchedule.objects.select_related("campus")
    serializer_class = BellScheduleSerializer
    filterset_fields = ["campus", "is_active"]
    search_fields = ["name"]
    ordering_fields = ["name"]
    required_permissions = _perms(retime=[MANAGE])

    @extend_schema(
        tags=["timetable"], summary="New bell times from a date (e.g. winter timings)",
        description="Moves the given periods, and every lesson in them, to new clock times from "
                    "effective_from. Past dates keep the old times. All or nothing: 409 "
                    "timetable_clash if a moved lesson would clash with one that isn't moving.",
        request=RetimeSerializer, responses={200: PeriodSerializer(many=True)},
    )
    @action(detail=True, methods=["post"])
    def retime(self, request, pk=None):
        schedule = self.get_object()
        serializer = RetimeSerializer(data=request.data, context={"schedule": schedule})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            periods = retime_schedule(
                schedule=schedule, effective_from=serializer.validated_data["effective_from"],
                new_times=serializer.validated_data["new_times"],
                visible_campus_ids=self.visible_campus_ids(), by=request.user,
            )
        return Response(PeriodSerializer(periods, many=True).data)

    def perform_destroy(self, instance):
        if TimetableEntry.objects.filter(period__schedule=instance).exists():
            raise ConflictError(
                "Lessons are scheduled in this bell schedule's periods. Remove those first.",
                code="in_use",
            )
        with transaction.atomic():
            for period in instance.periods.all():
                period.delete(deleted_by=self.request.user)
            super().perform_destroy(instance)


@_schema("period")
class PeriodViewSet(TimetableViewSet):
    """A bell schedule's periods. ``?schedule=<id>`` lists one schedule's day."""

    queryset = Period.objects.select_related("schedule")
    serializer_class = PeriodSerializer
    campus_field = "schedule__campus"
    filterset_fields = ["schedule", "schedule__campus", "is_break"]
    ordering_fields = ["start_time"]
    ordering = ["schedule", "start_time", "pk"]

    def campus_of(self, validated_data):
        schedule = validated_data.get("schedule")
        return schedule.campus if schedule is not None else None

    def perform_destroy(self, instance):
        if instance.timetable_entries.exists():
            raise ConflictError(
                "Lessons are scheduled in this period. Remove those first.", code="in_use"
            )
        super().perform_destroy(instance)

    def filter_queryset(self, queryset):
        # Today's bell times; ?include_ended=true adds times that were replaced.
        queryset = super().filter_queryset(queryset)
        if self.action == "list" and self.request.query_params.get("include_ended") != "true":
            queryset = queryset.filter(running_from(timezone.localdate()))
        return queryset


class TimetableEntryFilter(filters.FilterSet):
    section = filters.NumberFilter(field_name="teaching_assignment__section")
    teacher = filters.NumberFilter(field_name="teaching_assignment__teacher")
    subject = filters.NumberFilter(field_name="teaching_assignment__subject")
    campus = filters.NumberFilter(field_name="teaching_assignment__section__campus")
    academic_year = filters.NumberFilter(field_name="teaching_assignment__section__academic_year")
    date = filters.DateFilter(
        method="filter_date",
        label="Lessons that take place on this date: its weekday, within the "
              "section's academic year and, for term lessons, within the term.",
    )

    class Meta:
        model = TimetableEntry
        fields = ["teaching_assignment", "day_of_week", "period", "room", "term", "combined_group"]

    def filter_date(self, queryset, name, value):
        return entries_on(value, queryset)


_LESSON_FILTERS = [
    OpenApiParameter("date", str, required=True, description="YYYY-MM-DD"),
    OpenApiParameter("section", int), OpenApiParameter("teacher", int), OpenApiParameter("room", int),
]


@_schema("timetable entry")
class TimetableEntryViewSet(TimetableViewSet):
    """The weekly timetable. Filter by ``section``, ``teacher`` or ``room``
    for one class's, one teacher's or one room's week; ``date`` gives the
    lessons of one day.

    A write that would double-book a teacher, room or section is refused
    with 409 ``timetable_clash``; ``details.clashes`` lists what it hit.
    """

    queryset = TimetableEntry.objects.select_related(*ENTRY_RELATED)
    serializer_class = TimetableEntrySerializer
    campus_field = "teaching_assignment__section__campus"
    filterset_class = TimetableEntryFilter
    ordering_fields = ["day_of_week", "period__start_time"]
    ordering = ["day_of_week", "period__start_time", "pk"]
    required_permissions = _perms(
        day=[VIEW],
        hand_over=[MANAGE, "academics.manage_classes"],
        generate=[MANAGE],
    )
    SLOT_FIELDS = ("teaching_assignment", "day_of_week", "period", "room", "term")

    def get_permissions(self):
        # Teachers, students and parents read their own timetable without
        # any timetable permission, like the other /me/ endpoints.
        if self.action == "me":
            return [IsAuthenticated()]
        return super().get_permissions()

    def campus_of(self, validated_data):
        assignment = validated_data.get("teaching_assignment")
        return assignment.section.campus if assignment is not None else None

    # -- writes ------------------------------------------------------------
    def _slot(self, serializer):
        data, instance = serializer.validated_data, serializer.instance
        return {f: data[f] if f in data else getattr(instance, f, None)
                for f in (*self.SLOT_FIELDS, "valid_from", "valid_until")}

    def _check(self, *, organization_id, slot, group=None, exclude_pks=()):
        lock_and_check(
            organization_id=organization_id, group=group, exclude_pks=exclude_pks,
            visible_campus_ids=self.visible_campus_ids(),
            assignment=slot["teaching_assignment"], day_of_week=slot["day_of_week"],
            period=slot["period"], room=slot["room"], term=slot["term"],
            valid_from=slot["valid_from"], valid_until=slot["valid_until"],
        )

    def _check_room_size(self, serializer, slot, group=None, exclude_pks=()):
        """A combined class, or one big section, can outgrow its room. Refused
        unless the caller says it's on purpose (allow_over_capacity)."""
        room = slot["room"]
        if room is None or room.capacity is None or serializer.allow_over_capacity:
            return
        assignment = slot["teaching_assignment"]
        classes = [(assignment.section, assignment.subject_id)]
        if group is not None:
            classes += [(e.teaching_assignment.section, e.teaching_assignment.subject_id)
                        for e in TimetableEntry.objects.filter(combined_group=group)
                        .exclude(pk__in=exclude_pks).select_related("teaching_assignment__section")]
        students = sum(students_taking(section, subject).count() for section, subject in classes)
        if students > room.capacity:
            raise ConflictError(
                f"{room.name} seats {room.capacity}; this class has {students} students. "
                "Booking it anyway needs a deliberate override.",
                code="room_too_small",
            )

    def perform_create(self, serializer):
        target = serializer.combine_target
        slot = self._slot(serializer)
        # Campus first: a write to a campus the caller doesn't run is a 403,
        # never a 409 describing that campus's timetable.
        self.check_campus_allowed(self.campus_of(serializer.validated_data))
        org = self.get_target_organization_id()
        with transaction.atomic():
            if target is None:
                self._check(organization_id=org, slot=slot)
                self._check_room_size(serializer, slot)
                super().perform_create(serializer)
                self._continue(serializer.instance)
                return
            # Group the target first so the check sees it as the same class;
            # a clash rolls this back with everything else.
            if target.combined_group is None:
                before = snapshot(target)
                target.combined_group = uuid.uuid4()
                target.save(update_fields=["combined_group", "updated_at"])
                log_update(self.request, target, before=before, module=self.audit_module)
            self._check(organization_id=org, slot=slot, group=target.combined_group)
            self._check_room_size(serializer, slot, group=target.combined_group)
            serializer.validated_data["combined_group"] = target.combined_group
            super().perform_create(serializer)
            self._continue(serializer.instance)

    def _continue(self, entry):
        """Carry the lesson over new bell times already scheduled for its period."""
        continue_across_retimes(entry, by=self.request.user, visible_campus_ids=self.visible_campus_ids())

    def perform_update(self, serializer):
        """Changes to when, where or who apply from ``effective_from``
        (default today). A lesson that already ran before then isn't edited:
        it ends the day before and a new one carries on, so attendance taken
        earlier still points at what really happened. A combined class moves
        together."""
        instance, data = serializer.instance, serializer.validated_data
        effective_from = serializer.effective_from
        slot = self._slot(serializer)
        changes = {f: data[f] for f in self.SLOT_FIELDS if f in data and data[f] != getattr(instance, f)}
        group = instance.combined_group
        moving_group = group is not None and any(f != "teaching_assignment" for f in changes)
        members = (list(TimetableEntry.objects.filter(combined_group=group).filter(running_from(effective_from))
                        .exclude(pk=instance.pk).select_related(*ENTRY_RELATED))
                   if moving_group else [])
        member_pks = [m.pk for m in members]
        versioned = bool(changes) and has_run_before(instance, effective_from)
        if versioned and instance.valid_until is not None and instance.valid_until < effective_from:
            raise ValidationError({"effective_from": "The lesson has already ended by then."})

        self.check_campus_allowed(instance.teaching_assignment.section.campus_id)
        with transaction.atomic():
            checked = dict(slot, valid_from=max(effective_from, slot["valid_from"] or effective_from)) \
                if versioned else slot
            self._check(organization_id=instance.organization_id, slot=checked, group=group,
                        exclude_pks=[instance.pk, *member_pks])
            if "room" in changes or "teaching_assignment" in changes:
                self._check_room_size(serializer, checked, group=group, exclude_pks=[instance.pk])
            for member in members:
                member_slot = dict(checked, teaching_assignment=member.teaching_assignment)
                self._check(organization_id=member.organization_id, slot=member_slot, group=group,
                            exclude_pks=[instance.pk, *member_pks])

            if not versioned:
                super().perform_update(serializer)
                self._continue(serializer.instance)
                for member in members:
                    before = snapshot(member)
                    for field in self.SLOT_FIELDS[1:]:
                        setattr(member, field, getattr(instance, field))
                    member.save(update_fields=[*self.SLOT_FIELDS[1:], "updated_at"])
                    log_update(self.request, member, before=before, module=self.audit_module)
                return

            new_group = uuid.uuid4() if group is not None and moving_group else group
            slot_changes = {f: v for f, v in changes.items() if f != "teaching_assignment"}
            serializer.instance = supersede(
                instance, effective_from=effective_from, by=self.request.user,
                changes={**changes, "combined_group": new_group},
            )
            for member in members:
                supersede(member, effective_from=effective_from, by=self.request.user,
                          changes={**slot_changes, "combined_group": new_group})
            self._continue(serializer.instance)

    def perform_destroy(self, instance):
        """A lesson that never ran is removed. One that has run is ended the
        day before ``?effective_from=`` (default today), so its past stays;
        its substitutes and changes from then on are removed with it."""
        raw = self.request.query_params.get("effective_from")
        effective_from = _query_date(self.request, required=False, name="effective_from") if raw \
            else timezone.localdate()
        group = instance.combined_group
        with transaction.atomic():
            if not has_run_before(instance, effective_from):
                super().perform_destroy(instance)
                if group is not None:
                    rest = list(TimetableEntry.objects.filter(combined_group=group))
                    if len(rest) == 1 and not has_run_before(rest[0], timezone.localdate()):
                        # One section left: no longer a combined class.
                        rest[0].combined_group = None
                        rest[0].save(update_fields=["combined_group", "updated_at"])
                return
            if instance.valid_until is not None and instance.valid_until < effective_from:
                raise ConflictError("The lesson has already ended.", code="already_ended")
            before = snapshot(instance)
            instance.valid_until = effective_from - timedelta(days=1)
            instance.save(update_fields=["valid_until", "updated_at"])
            for change in instance.changes.filter(date__gte=effective_from):
                change.delete(deleted_by=self.request.user)
            log_update(self.request, instance, before=before, module=self.audit_module)

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        params = self.request.query_params
        # The list shows the timetable from today on; ?date= picks a day, and
        # ?include_ended=true adds lessons that have ended (history).
        if self.action == "list" and "date" not in params and params.get("include_ended") != "true":
            queryset = queryset.filter(running_from(timezone.localdate()))
        return queryset

    # -- actions -----------------------------------------------------------
    @extend_schema(tags=["timetable"], summary="The lessons of one day, with that day's changes",
                   description="Substitutes, room changes and cancellations applied. `teacher` "
                               "includes lessons they cover; `room` includes lessons moved into it.",
                   parameters=_LESSON_FILTERS, responses={200: LessonSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def day(self, request):
        day = _query_date(request)
        lessons = lessons_on(
            day, organization_id=request.user.organization_id, entries=self.get_queryset(),
            section=_query_id(request, "section"), teacher=_query_id(request, "teacher"),
            room=_query_id(request, "room"),
        )
        return Response(LessonSerializer(lessons, many=True).data)

    @extend_schema(
        tags=["timetable"], summary="Hand lessons over to another teacher",
        description="Moves every lesson of the given assignments to the teacher, creating their "
                    "assignments as needed. All or nothing: 409 timetable_clash lists every clash.",
        request=HandOverSerializer, responses={200: TeachingAssignmentSerializer(many=True)},
    )
    @action(detail=False, methods=["post"], url_path="hand-over")
    def hand_over(self, request):
        serializer = HandOverSerializer(
            data=request.data, context={"organization_id": self.get_target_organization_id()}
        )
        serializer.is_valid(raise_exception=True)
        assignments = serializer.validated_data["teaching_assignments"]
        for assignment in assignments:
            self.check_campus_allowed(assignment.section.campus_id)
        with transaction.atomic():
            new = hand_over(assignments=assignments, teacher=serializer.validated_data["teacher"],
                            on=serializer.validated_data.get("on"),
                            visible_campus_ids=self.visible_campus_ids(), by=request.user)
        return Response(TeachingAssignmentSerializer(new, many=True).data)

    @extend_schema(
        tags=["timetable"], summary="Generate the weekly timetable",
        description="Fills each teaching assignment up to its periods_per_week, from valid_from "
                    "(default today). With dry_run (the default) it only returns the plan; with "
                    "dry_run false it saves it.",
        request=GenerateSerializer, responses={200: None, 201: None},
    )
    @action(detail=False, methods=["post"])
    def generate(self, request):
        serializer = GenerateSerializer(
            data=request.data, context={"organization_id": self.get_target_organization_id()}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        self.check_campus_allowed(data["schedule"].campus_id)
        plan = plan_timetable(sections=data["sections"], schedule=data["schedule"],
                              days=data["days"], term=data.get("term"), start=data.get("valid_from"))

        created = []
        if not data["dry_run"]:
            with transaction.atomic():
                for proposal in plan.proposals:
                    # The plan was made without locks; check each lesson
                    # again under them, as a hand-made one would be.
                    first, _ = span(academic_year=proposal.assignment.section.academic_year,
                                    term=data.get("term"))
                    valid_from = plan.start if plan.start > first else None
                    lock_and_check(
                        organization_id=proposal.assignment.organization_id,
                        assignment=proposal.assignment, day_of_week=proposal.day,
                        period=proposal.period, room=proposal.room, term=data.get("term"),
                        valid_from=valid_from, visible_campus_ids=self.visible_campus_ids(),
                    )
                    entry = TimetableEntry.objects.create(
                        organization_id=proposal.assignment.organization_id,
                        teaching_assignment=proposal.assignment, day_of_week=proposal.day,
                        period=proposal.period, room=proposal.room, term=data.get("term"),
                        valid_from=valid_from,
                    )
                    log_create(request, entry, module=self.audit_module)
                    continue_across_retimes(entry, by=request.user,
                                            visible_campus_ids=self.visible_campus_ids())
                    created.append(entry.pk)

        lessons = [
            {
                "id": created[i] if created else None,
                "teaching_assignment": p.assignment.pk,
                "section": p.assignment.section_id,
                "section_name": p.assignment.section.display_name,
                "subject_name": p.assignment.subject.name,
                "teacher_name": p.assignment.teacher.full_name,
                "day_of_week": p.day,
                "period": p.period.pk,
                "period_name": p.period.name,
                "start_time": p.period.start_time.strftime("%H:%M"),
                "end_time": p.period.end_time.strftime("%H:%M"),
                "room": p.room.pk if p.room else None,
                "room_name": p.room.name if p.room else None,
            }
            for i, p in enumerate(plan.proposals)
        ]
        return Response(
            {"dry_run": data["dry_run"], "created": len(created), "lessons": lessons,
             "unplaced": plan.unplaced},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        tags=["timetable"], summary="My timetable",
        description="For a teacher, their lessons (and the ones they cover on a date). For a "
                    "student, their class's lessons: compulsory subjects and the electives they "
                    "take (every elective while none is recorded). For a parent, a child's, "
                    "chosen with `student` when there is more than one. Without `date`, the "
                    "week's lessons; with it, that day's, changes applied.",
        parameters=[
            OpenApiParameter("date", str, description="YYYY-MM-DD"),
            OpenApiParameter("as", str, enum=["teacher", "student", "parent"],
                             description="Which of your profiles, when you have several."),
            OpenApiParameter("student", int, description="Parents: which child."),
        ],
        responses={200: None},
    )
    @action(detail=False, methods=["get"])
    def me(self, request):
        day = _query_date(request, required=False)
        wanted = request.query_params.get("as")
        user = request.user
        if wanted not in (None, "teacher", "student", "parent"):
            raise ValidationError({"as": "One of teacher, student, parent."})

        # The first profile the account has, in this order, unless ``as`` picks one.
        finders = {"teacher": staff_member_for_user, "student": student_for_user,
                   "parent": parent_for_user}
        role, profile = None, None
        for name, find in finders.items():
            if wanted in (None, name):
                profile = find(user)
                if profile is not None:
                    role = name
                    break
        if role is None:
            raise NotFound("No teacher, student or parent profile is linked to your account.")

        base = TimetableEntry.objects.filter(organization_id=user.organization_id)
        body = {"as": role, "date": day.isoformat() if day else None}

        if role == "teacher":
            if day is not None:
                lessons = lessons_on(day, organization_id=user.organization_id, teacher=profile, entries=base)
                return Response({**body, "lessons": LessonSerializer(lessons, many=True).data})
            return Response({**body, "lessons": self._week(base.filter(teaching_assignment__teacher=profile))})

        student = profile if role == "student" else self._child(profile)
        body["student"] = student.pk
        enrollment = get_current_enrollment(student)
        if enrollment is None or enrollment.section_id is None:
            return Response({**body, "lessons": []})
        section = enrollment.section
        entries = base.filter(teaching_assignment__section=section)
        chosen = chosen_elective_ids(enrollment)
        if chosen:
            others = CurriculumSubject.objects.filter(
                program_id=section.program_id, level=section.level, is_elective=True
            ).exclude(subject_id__in=chosen).values("subject_id")
            entries = entries.exclude(teaching_assignment__subject_id__in=others)
        if day is not None:
            lessons = lessons_on(day, organization_id=user.organization_id, section=section, entries=entries)
            return Response({**body, "lessons": LessonSerializer(lessons, many=True).data})
        return Response({**body, "lessons": self._week(entries)})

    def _week(self, entries):
        entries = entries.select_related(*ENTRY_RELATED).order_by("day_of_week", "period__start_time", "pk")
        return TimetableEntrySerializer(entries, many=True, context=self.get_serializer_context()).data

    def _child(self, parent):
        links = list(links_for_parent(parent))
        wanted = _query_id(self.request, "student")
        if wanted is not None:
            links = [link for link in links if link.student_id == wanted]
            if not links:
                raise NotFound("That student isn't linked to you.")
        elif len(links) > 1:
            raise ValidationError({"student": "You have several children linked; choose one."})
        if not links:
            raise NotFound("No student is linked to you.")
        return links[0].student


class LessonChangeFilter(filters.FilterSet):
    date_from = filters.DateFilter(field_name="date", lookup_expr="gte")
    date_to = filters.DateFilter(field_name="date", lookup_expr="lte")
    section = filters.NumberFilter(field_name="entry__teaching_assignment__section")
    teacher = filters.NumberFilter(field_name="entry__teaching_assignment__teacher",
                                   label="The lesson's regular teacher.")

    class Meta:
        model = LessonChange
        fields = ["entry", "date", "substitute_teacher", "room", "is_cancelled"]


@_schema("lesson change")
class LessonChangeViewSet(TimetableViewSet):
    """One lesson on one date: a substitute teacher, another room, or
    cancelled. The substitute or room must be free then (409 otherwise)."""

    queryset = LessonChange.objects.select_related(
        "entry__period", "entry__teaching_assignment__section__program",
        "entry__teaching_assignment__subject", "entry__teaching_assignment__teacher",
        "substitute_teacher", "room",
    )
    serializer_class = LessonChangeSerializer
    campus_field = "entry__teaching_assignment__section__campus"
    filterset_class = LessonChangeFilter
    ordering_fields = ["date"]
    ordering = ["date", "entry__period__start_time", "pk"]

    def campus_of(self, validated_data):
        entry = validated_data.get("entry")
        return entry.teaching_assignment.section.campus_id if entry is not None else None

    def _check(self, serializer):
        data, instance = serializer.validated_data, serializer.instance
        current = lambda field: data[field] if field in data else getattr(instance, field, None)  # noqa: E731
        entry = current("entry")
        self.check_campus_allowed(entry.teaching_assignment.section.campus_id)
        teacher, room = current("substitute_teacher"), current("room")
        lock(teachers=[teacher.pk] if teacher else [], rooms=[room.pk] if room else [])
        raise_if_clashes(
            find_date_clashes(organization_id=entry.organization_id, entry=entry, day=current("date"),
                              teacher=teacher, room=room),
            self.visible_campus_ids(),
        )

    # A combined class is one lesson in one room with one teacher: a change
    # to one section's lesson is a change to every section's.
    MIRRORED = ("is_cancelled", "substitute_teacher", "room", "note")

    def _classmates(self, change_or_entry, day):
        """The other sections' lessons of this combined class on ``day``, by
        the calendar (so a make-up day finds them too)."""
        entry = getattr(change_or_entry, "entry", change_or_entry)
        if entry.combined_group is None:
            return []
        members = TimetableEntry.objects.filter(combined_group=entry.combined_group).exclude(pk=entry.pk)
        return [lesson.entry for lesson in lessons_on(day, organization_id=entry.organization_id, entries=members)]

    def perform_create(self, serializer):
        data = serializer.validated_data
        with transaction.atomic():
            self._check(serializer)
            members = self._classmates(data["entry"], data["date"])
            taken = LessonChange.objects.filter(entry__in=members, date=data["date"])
            if taken.exists():
                raise ConflictError(
                    "Another section of this combined class already has a change that day. Edit that one.",
                    code="combined_class",
                )
            super().perform_create(serializer)
            for member in members:
                copy = LessonChange.objects.create(
                    organization_id=member.organization_id, entry=member, date=data["date"],
                    **{f: getattr(serializer.instance, f) for f in self.MIRRORED},
                )
                log_create(self.request, copy, module=self.audit_module)

    def perform_update(self, serializer):
        with transaction.atomic():
            self._check(serializer)
            super().perform_update(serializer)
            change = serializer.instance
            for other in LessonChange.objects.filter(
                entry__in=self._classmates(change, change.date), date=change.date
            ):
                before = snapshot(other)
                for field in self.MIRRORED:
                    setattr(other, field, getattr(change, field))
                other.save(update_fields=[*self.MIRRORED, "updated_at"])
                log_update(self.request, other, before=before, module=self.audit_module)

    def perform_destroy(self, instance):
        with transaction.atomic():
            others = list(LessonChange.objects.filter(
                entry__in=self._classmates(instance, instance.date), date=instance.date
            ))
            super().perform_destroy(instance)
            for other in others:
                log_delete(self.request, other, module=self.audit_module)
                other.delete(deleted_by=self.request.user)
