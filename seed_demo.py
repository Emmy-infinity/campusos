"""
Seed the local dev database with a lecturer + sample courses/exam/rubric.

Run: python seed_demo.py
Requires: DJANGO_SETTINGS_MODULE=campusOs.settings
"""
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "campusOs.settings")
django.setup()

from campusos.models import (
    User,
    LecturerProfile,
    StudentProfile,
    Course,
    QuestionBank,
    Question,
    Exam,
    ExamQuestion,
    Rubric,
    Document,
    UserAICredit,
)


def main():
    # ── 1. Lecturer ────────────────────────────────────────────
    lect, created = User.objects.get_or_create(
        username="lecturer1",
        defaults={
            "email": "lecturer1@example.com",
            "first_name": "Jane",
            "last_name": "Doe",
        },
    )
    lect.set_password("lecturer123")
    lect.role = "LECTURER"
    lect.is_active = True
    lect.save()

    lect_profile, _ = LecturerProfile.objects.get_or_create(
        user=lect,
        defaults={
            "staff_id": "STF-001",
            "department": "Computer Science",
            "designation": "Senior Lecturer",
        },
    )
    print(f"[+] Lecturer: {lect.username} / lecturer123")

    # ── 2. Courses ─────────────────────────────────────────────
    c1, _ = Course.objects.get_or_create(
        code="CS301",
        defaults={
            "title": "Data Structures and Algorithms",
            "department": "Computer Science",
            "lecturer": lect_profile,
            "description": "Core algorithms course",
        },
    )
    c2, _ = Course.objects.get_or_create(
        code="CS402",
        defaults={
            "title": "Machine Learning",
            "department": "Computer Science",
            "lecturer": lect_profile,
            "description": "Introduction to ML",
        },
    )
    print(f"[+] Courses: {c1.code}, {c2.code}")

    # ── 3. Question bank ───────────────────────────────────────
    qb, _ = QuestionBank.objects.get_or_create(
        course=c1,
        title="CS301 Midterm Bank",
        defaults={
            "created_by": lect,
            "description": "Questions for midterm exam",
        },
    )
    print(f"[+] Question bank: {qb.title}")

    # ── 4. Rubric ──────────────────────────────────────────────
    rubric, _ = Rubric.objects.get_or_create(
        title="Research Paper Rubric",
        defaults={
            "created_by": lect,
            "course": c2,
            "criteria": [
                {"name": "Clarity", "description": "Clear writing", "weight": 25},
                {"name": "Depth", "description": "Depth of analysis", "weight": 35},
                {"name": "Sources", "description": "Quality of references", "weight": 20},
                {"name": "Formatting", "description": "Correct citation style", "weight": 20},
            ],
        },
    )
    print(f"[+] Rubric: {rubric.title}")

    # ── 5. Questions ───────────────────────────────────────────
    q1, _ = Question.objects.get_or_create(
        bank=qb,
        text="What is the time complexity of binary search?",
        defaults={
            "question_type": "MCQ",
            "options": ["O(1)", "O(log n)", "O(n)", "O(n log n)"],
            "correct_option": 1,
            "marks": 2,
        },
    )
    q2, _ = Question.objects.get_or_create(
        bank=qb,
        text="Which data structure uses LIFO?",
        defaults={
            "question_type": "MCQ",
            "options": ["Queue", "Stack", "Heap", "Tree"],
            "correct_option": 1,
            "marks": 2,
        },
    )
    print(f"[+] Questions: {q1.id}, {q2.id}")

    # ── 6. Exam (published) ────────────────────────────────────
    exam, _ = Exam.objects.get_or_create(
        course=c1,
        title="CS301 Midterm",
        defaults={
            "duration_minutes": 90,
            "created_by": lect,
            "is_published": True,
        },
    )
    ExamQuestion.objects.get_or_create(exam=exam, question=q1, defaults={"order": 1})
    ExamQuestion.objects.get_or_create(exam=exam, question=q2, defaults={"order": 2})
    print(f"[+] Exam: {exam.title} (published={exam.is_published})")

    # ── 7. Tokens ──────────────────────────────────────────────
    credit, _ = UserAICredit.objects.get_or_create(user=lect)
    if credit.remaining_tokens < 100000:
        credit.add_tokens(100000 - credit.remaining_tokens)
    print(f"[+] Lecturer tokens: {credit.remaining_tokens}")

    # ── 8. Reassign a few admin docs to the lecturer (optional) ─
    admin_docs = Document.objects.filter(uploaded_by__role="ADMIN")[:3]
    count = 0
    for d in admin_docs:
        d.uploaded_by = lect
        d.save(update_fields=["uploaded_by"])
        count += 1
    print(f"[+] Reassigned {count} documents to lecturer")

    print("\n--- Seeding complete ---")
    print("Login as: lecturer1 / lecturer123")


if __name__ == "__main__":
    main()
