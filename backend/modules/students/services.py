"""Write operations for students and their enrollments.

Every change to where a student is or what state they are in goes through
here, so the student row and its enrollment history can never disagree:
an active or suspended student has exactly one open enrollment, at the
student's campus; a graduated or withdrawn student has none.
"""
from django.db import transaction
from django.utils import timezone

from core.audit.models import AuditLog
from core.audit.services import log, snapshot
from core.common.exceptions import ConflictError, ServiceError

from .models import Enrollment, Student
from .signals import student_graduated

# Which status a student may move to from each status. Graduated and
# withdrawn are final: a returning student comes back through a new admission.
ALLOWED_TRANSITIONS = {
    Student.Status.ACTIVE: {
        Student.Status.SUSPENDED,
        Student.Status.GRADUATED,
        Student.Status.WITHDRAWN,
    },
    Student.Status.SUSPENDED: {Student.Status.ACTIVE, Student.Status.WITHDRAWN},
    Student.Status.GRADUATED: set(),
    Student.Status.WITHDRAWN: set(),
}

# How a status change closes the open enrollment, if it does.
_CLOSING_STATUS = {
    Student.Status.GRADUATED: Enrollment.Status.COMPLETED,
    Student.Status.WITHDRAWN: Enrollment.Status.WITHDRAWN,
}


def _ensure_same_organization(organization_id, campus) -> None:
    if campus.organization_id != organization_id:
        raise ServiceError("Campus belongs to a different organization.", code="invalid_campus")


def _open_enrollment(student: Student) -> Enrollment:
    enrollment = (
        Enrollment.objects.select_for_update()
        .filter(student=student, status=Enrollment.Status.ACTIVE)
        .first()
    )
    if enrollment is None:  # pragma: no cover - guarded by the invariant above
        raise ConflictError("Student has no open enrollment.", code="no_open_enrollment")
    return enrollment


def _close(enrollment: Enrollment, status: str, on_date, reason: str) -> None:
    if enrollment.started_on > timezone.localdate() and enrollment.section_id is not None:
        raise ConflictError(
            f"A move to {enrollment.section.display_name} on {enrollment.started_on} is scheduled. "
            "Cancel it first by placing the student back in their current class.",
            code="scheduled_move",
        )
    if on_date < enrollment.started_on:
        raise ServiceError(
            f"The date cannot be before the enrollment started ({enrollment.started_on}).",
            code="invalid_date",
        )
    enrollment.status = status
    enrollment.ended_on = on_date
    enrollment.end_reason = reason
    enrollment.save(update_fields=["status", "ended_on", "end_reason", "updated_at"])


@transaction.atomic
def create_student(
    *,
    organization_id: int,
    campus,
    student_number: str,
    created_by=None,
    **fields,
) -> Student:
    """Create a student and open their first enrollment at ``campus``."""
    _ensure_same_organization(organization_id, campus)

    student = Student(
        organization_id=organization_id,
        campus=campus,
        student_number=student_number,
        **fields,
    )
    # full_clean runs the partial unique constraints in Python, so a duplicate
    # student number is a 400 on every database — MySQL included, which
    # cannot enforce them itself.
    student.full_clean()
    student.save()

    Enrollment.objects.create(
        organization_id=organization_id,
        student=student,
        campus=campus,
        started_on=student.admitted_on,
    )

    log(AuditLog.Action.CREATE, instance=student, module="students", actor=created_by)
    return student


@transaction.atomic
def transfer_student(*, student: Student, to_campus, on_date=None, reason="", by=None) -> Student:
    """Move a student to another campus of the same organization."""
    student = Student.objects.select_for_update().get(pk=student.pk)
    _ensure_same_organization(student.organization_id, to_campus)
    if not student.is_enrolled:
        raise ConflictError(
            f"A {student.get_status_display().lower()} student cannot be transferred.",
            code="invalid_status",
        )
    if to_campus.pk == student.campus_id:
        raise ServiceError("The student is already at this campus.", code="same_campus")

    on_date = on_date or timezone.localdate()
    before = snapshot(student)

    _close(_open_enrollment(student), Enrollment.Status.TRANSFERRED, on_date, reason)
    Enrollment.objects.create(
        organization_id=student.organization_id,
        student=student,
        campus=to_campus,
        started_on=on_date,
    )
    student.campus = to_campus
    student.save(update_fields=["campus", "updated_at"])

    log(
        AuditLog.Action.UPDATE,
        instance=student,
        module="students",
        actor=by,
        changes={"campus": {"before": before["campus"], "after": to_campus.pk}},
        metadata={"operation": "transfer", "reason": reason, "on": str(on_date)},
    )
    return student


@transaction.atomic
def change_student_status(
    *, student: Student, status: str, on_date=None, reason="", by=None
) -> Student:
    """Suspend, reactivate, graduate or withdraw a student."""
    student = Student.objects.select_for_update().get(pk=student.pk)
    if status not in ALLOWED_TRANSITIONS.get(student.status, set()):
        raise ConflictError(
            f"Cannot change a student from {student.status} to {status}.",
            code="invalid_transition",
        )

    before = student.status
    closed = None
    if status in _CLOSING_STATUS:
        closed = _open_enrollment(student)
        _close(closed, _CLOSING_STATUS[status], on_date or timezone.localdate(), reason)

    student.status = status
    student.save(update_fields=["status", "updated_at"])
    if status == Student.Status.GRADUATED:
        student_graduated.send(sender=Student, student=student, enrollment=closed,
                               on_date=on_date or timezone.localdate(), by=by)

    log(
        AuditLog.Action.UPDATE,
        instance=student,
        module="students",
        actor=by,
        changes={"status": {"before": before, "after": status}},
        metadata={"operation": "status_change", "reason": reason},
    )
    return student


def _carry_electives(old: Enrollment, new: Enrollment) -> None:
    """A move to another section of the same program and level (A → B)
    keeps the student's electives; a promotion starts them afresh, because
    the next level has its own curriculum."""
    from modules.academics.models import StudentElective

    old_section, new_section = old.section, new.section
    if (old_section.program_id, old_section.level) != (new_section.program_id, new_section.level):
        return
    StudentElective.objects.bulk_create(
        StudentElective(organization_id=new.organization_id, enrollment=new, subject_id=subject_id,
                        started_on=new.started_on)
        for subject_id in old.electives.filter(ended_on__isnull=True).values_list("subject_id", flat=True)
    )


def _check_capacity(section, day, allow_over_capacity, exclude_student=None) -> None:
    """A full class refuses more students unless the caller says it's on
    purpose: schools do go over capacity, but never by accident."""
    if section.capacity is None or allow_over_capacity:
        return
    placed = Enrollment.objects.on(day).filter(section=section).exclude(student_id=exclude_student).count()
    if placed >= section.capacity:
        raise ConflictError(
            f"{section.display_name} is full ({placed} of {section.capacity}) on {day}. "
            "Placing more students needs a deliberate override.",
            code="over_capacity",
        )


def _previous_enrollment(enrollment: Enrollment) -> Enrollment | None:
    """The class a scheduled move leaves: closed as moved on the day the
    move starts."""
    return (
        Enrollment.objects.select_for_update()
        .filter(student_id=enrollment.student_id, status=Enrollment.Status.MOVED,
                ended_on=enrollment.started_on, campus_id=enrollment.campus_id)
        .exclude(pk=enrollment.pk).order_by("-started_on", "-pk").first()
    )


@transaction.atomic
def place_student(*, student: Student, section, on_date=None, reason="", by=None,
                  allow_over_capacity=False) -> Student:
    """Put a student in a section (class group), or move them to another.

    The first placement of an enrollment fills in its section. Any later move —
    promotion to the next grade, a new academic year, a section change —
    closes the current enrollment as ``moved`` and opens a new one, so the
    history shows every class the student has been in.

    ``on_date`` may be in the future: a promotion recorded in Chaitra for
    Baisakh leaves the student in their class until then (see
    ``Enrollment.objects.on``). Placing again while such a move is pending
    revises it; placing back into the class they're leaving cancels it.
    """
    student = Student.objects.select_for_update().get(pk=student.pk)
    if section.organization_id != student.organization_id:
        raise ServiceError("Section belongs to a different organization.", code="invalid_section")
    if not student.is_enrolled:
        raise ConflictError(
            f"A {student.get_status_display().lower()} student cannot be placed.",
            code="invalid_status",
        )
    if section.campus_id != student.campus_id:
        raise ServiceError(
            "The section is at another campus. Transfer the student first.",
            code="different_campus",
        )

    today = timezone.localdate()
    current = _open_enrollment(student)
    # A first placement fills in the open enrollment from its own start.
    effective = current.started_on if current.section_id is None else (on_date or today)

    # Placing ahead into next year's sections is normal (promotions are set
    # up before the year starts); placing into a year that is already over
    # is always a mistake.
    year = section.academic_year
    if year.end_date < (on_date or today):
        raise ServiceError(
            f"The academic year {year.name} ended on {year.end_date}.", code="year_ended"
        )

    before = current.section_id
    pending = current.section_id is not None and current.started_on > today
    if pending:
        # A move that hasn't happened yet is revised, not stacked on.
        previous = _previous_enrollment(current)
        if previous is not None and previous.section_id == section.pk:
            current.electives.all().delete()
            current.delete()
            previous.status, previous.ended_on, previous.end_reason = Enrollment.Status.ACTIVE, None, ""
            previous.save(update_fields=["status", "ended_on", "end_reason", "updated_at"])
            log(AuditLog.Action.UPDATE, instance=student, module="students", actor=by,
                changes={"section": {"before": before, "after": section.pk}},
                metadata={"operation": "cancel_scheduled_move", "reason": reason})
            return student
        if previous is not None and effective < previous.started_on:
            raise ServiceError(
                f"The date cannot be before the current class started ({previous.started_on}).",
                code="invalid_date",
            )
        _check_capacity(section, effective, allow_over_capacity, exclude_student=student.pk)
        if previous is not None:
            previous.ended_on = effective
            previous.end_reason = reason or previous.end_reason
            previous.save(update_fields=["ended_on", "end_reason", "updated_at"])
        current.electives.all().delete()
        current.section, current.started_on = section, effective
        current.save(update_fields=["section", "started_on", "updated_at"])
        if previous is not None:
            _carry_electives(previous, current)
    else:
        if current.section_id == section.pk:
            raise ServiceError("The student is already in this section.", code="same_section")
        _check_capacity(section, effective, allow_over_capacity, exclude_student=student.pk)
        if current.section_id is None:
            current.section = section
            current.save(update_fields=["section", "updated_at"])
        else:
            _close(current, Enrollment.Status.MOVED, effective, reason)
            new = Enrollment.objects.create(
                organization_id=student.organization_id,
                student=student,
                campus_id=student.campus_id,
                section=section,
                started_on=effective,
            )
            _carry_electives(current, new)

    log(
        AuditLog.Action.UPDATE,
        instance=student,
        module="students",
        actor=by,
        changes={"section": {"before": before, "after": section.pk}},
        metadata={"operation": "place", "reason": reason, "on": str(effective)},
    )
    return student


@transaction.atomic
def promote_section(*, section, to_section, exclude=(), on_date=None, reason="", by=None,
                    allow_over_capacity=False) -> list[Student]:
    """Move everyone in ``section`` (except ``exclude``) to ``to_section``:
    the end-of-year promotion of a whole class, or merging two sections.

    All or nothing. Each student goes through ``place_student``, so the same
    rules apply; if any student can't be moved, nobody is, and the error
    lists each student and why.
    """
    from modules.students.selectors import students_in_section

    students = list(students_in_section(section).exclude(pk__in=exclude).order_by("pk"))
    if not students:
        raise ServiceError("There are no students to move.", code="nothing_to_move")

    moved, failures = [], []
    for student in students:
        try:
            with transaction.atomic():
                moved.append(place_student(
                    student=student, section=to_section, on_date=on_date, reason=reason, by=by,
                    allow_over_capacity=allow_over_capacity,
                ))
        except ServiceError as exc:
            failures.append({"student": student.pk, "student_number": student.student_number,
                             "code": exc.code, "message": str(exc.detail)})
    if failures:
        raise ConflictError(
            f"{len(failures)} of {len(students)} students can't be moved; nobody was moved.",
            code="promotion_failed",
            details={"students": failures},
        )
    return moved
