# /// script
# requires-python = ">=3.10"
# ///
"""
IPC Handler — Bidirectional Worker Communication

Enables workers to ask questions and receive answers without losing context.
Uses atomic file operations to prevent race conditions.

Protocol:
    1. Worker writes question to .company/ipc/{worker-id}.question
    2. Dispatcher/human sees question, writes answer to {worker-id}.answer
    3. Worker reads answer and continues execution
    4. Files cleaned up after worker completes

Based on patterns from github.com/bassimeledath/dispatch
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class QuestionStatus(Enum):
    """Status of a question in the IPC system."""

    PENDING = "pending"
    ANSWERED = "answered"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass
class Question:
    """A question from a worker to the dispatcher/human."""

    question_id: str
    worker_id: str
    task_id: str
    text: str
    context: str = ""
    options: list[str] = field(default_factory=list)
    status: QuestionStatus = QuestionStatus.PENDING
    asked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    answered_at: datetime | None = None
    answer: str | None = None
    timeout_seconds: int = 300  # 5 minute default


@dataclass
class Answer:
    """An answer to a worker's question."""

    question_id: str
    worker_id: str
    text: str
    answered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    answered_by: str = "human"  # or "auto" or "dispatcher"


class IPCHandler:
    """
    Manages bidirectional communication between workers and dispatcher.

    Features:
    - Atomic file operations (no corruption)
    - Timeout handling for unanswered questions
    - Multiple question support per worker
    - Integration with escalation system
    """

    def __init__(self, project_root: Path | None = None):
        self.project_root = project_root or Path.cwd()
        self.ipc_dir = self.project_root / ".company" / "ipc"
        self.ipc_dir.mkdir(parents=True, exist_ok=True)

    def ask_question(
        self,
        worker_id: str,
        task_id: str,
        text: str,
        context: str = "",
        options: list[str] | None = None,
        timeout_seconds: int = 300,
    ) -> Question:
        """
        Worker asks a question.

        Args:
            worker_id: Worker identifier
            task_id: Task identifier
            text: Question text
            context: Additional context
            options: Suggested answer options
            timeout_seconds: How long to wait for answer

        Returns:
            Question object
        """
        question_id = f"q-{worker_id}-{int(time.time())}"

        question = Question(
            question_id=question_id,
            worker_id=worker_id,
            task_id=task_id,
            text=text,
            context=context,
            options=options or [],
            timeout_seconds=timeout_seconds,
        )

        # Write question file
        question_file = self.ipc_dir / f"{worker_id}.question"
        self._atomic_write(question_file, self._serialize_question(question))

        return question

    def get_pending_questions(self) -> list[Question]:
        """Get all pending questions from workers."""
        questions = []
        for qfile in self.ipc_dir.glob("*.question"):
            try:
                question = self._load_question(qfile)
                if question and question.status == QuestionStatus.PENDING:
                    # Check for timeout
                    if self._is_expired(question):
                        question.status = QuestionStatus.EXPIRED
                        self._atomic_write(qfile, self._serialize_question(question))
                    else:
                        questions.append(question)
            except Exception:
                continue
        return sorted(questions, key=lambda q: q.asked_at)

    def get_question(self, worker_id: str) -> Question | None:
        """Get the current question for a worker."""
        question_file = self.ipc_dir / f"{worker_id}.question"
        if not question_file.exists():
            return None
        return self._load_question(question_file)

    def answer_question(
        self,
        worker_id: str,
        answer_text: str,
        answered_by: str = "human",
    ) -> bool:
        """
        Answer a worker's question.

        Args:
            worker_id: Worker identifier
            answer_text: Answer text
            answered_by: Who answered (human/auto/dispatcher)

        Returns:
            True if successful
        """
        question_file = self.ipc_dir / f"{worker_id}.question"
        if not question_file.exists():
            return False

        question = self._load_question(question_file)
        if not question or question.status != QuestionStatus.PENDING:
            return False

        # Update question status
        question.status = QuestionStatus.ANSWERED
        question.answered_at = datetime.now(timezone.utc)
        question.answer = answer_text
        self._atomic_write(question_file, self._serialize_question(question))

        # Write answer file (worker polls this)
        answer = Answer(
            question_id=question.question_id,
            worker_id=worker_id,
            text=answer_text,
            answered_by=answered_by,
        )
        answer_file = self.ipc_dir / f"{worker_id}.answer"
        self._atomic_write(answer_file, self._serialize_answer(answer))

        return True

    def wait_for_answer(
        self,
        worker_id: str,
        poll_interval: float = 1.0,
        timeout_seconds: int | None = None,
    ) -> str | None:
        """
        Worker waits for an answer to their question.

        Args:
            worker_id: Worker identifier
            poll_interval: Seconds between polls
            timeout_seconds: Override question timeout

        Returns:
            Answer text or None if timeout/cancelled
        """
        question = self.get_question(worker_id)
        if not question:
            return None

        timeout = timeout_seconds or question.timeout_seconds
        start = time.time()

        while (time.time() - start) < timeout:
            answer_file = self.ipc_dir / f"{worker_id}.answer"
            if answer_file.exists():
                try:
                    answer = self._load_answer(answer_file)
                    if answer:
                        return answer.text
                except Exception:
                    pass

            # Check if question was cancelled/expired
            question = self.get_question(worker_id)
            if not question or question.status in (
                QuestionStatus.CANCELLED,
                QuestionStatus.EXPIRED,
            ):
                return None

            time.sleep(poll_interval)

        # Timeout - mark question expired
        question_file = self.ipc_dir / f"{worker_id}.question"
        if question_file.exists():
            question = self._load_question(question_file)
            if question and question.status == QuestionStatus.PENDING:
                question.status = QuestionStatus.EXPIRED
                self._atomic_write(question_file, self._serialize_question(question))

        return None

    def cancel_question(self, worker_id: str) -> bool:
        """Cancel a pending question."""
        question_file = self.ipc_dir / f"{worker_id}.question"
        if not question_file.exists():
            return False

        question = self._load_question(question_file)
        if not question:
            return False

        question.status = QuestionStatus.CANCELLED
        self._atomic_write(question_file, self._serialize_question(question))
        return True

    def cleanup_worker(self, worker_id: str) -> None:
        """Clean up IPC files for a completed worker."""
        for suffix in (".question", ".answer"):
            filepath = self.ipc_dir / f"{worker_id}{suffix}"
            if filepath.exists():
                try:
                    filepath.unlink()
                except Exception:
                    pass

    def cleanup_expired(self, max_age_seconds: int = 3600) -> int:
        """
        Clean up expired/old IPC files.

        Args:
            max_age_seconds: Remove files older than this

        Returns:
            Number of files cleaned up
        """
        cleaned = 0
        now = time.time()

        for filepath in self.ipc_dir.glob("*.*"):
            try:
                if (now - filepath.stat().st_mtime) > max_age_seconds:
                    filepath.unlink()
                    cleaned += 1
            except Exception:
                pass

        return cleaned

    def _is_expired(self, question: Question) -> bool:
        """Check if a question has expired."""
        age = (datetime.now(timezone.utc) - question.asked_at).total_seconds()
        return age > question.timeout_seconds

    def _serialize_question(self, question: Question) -> str:
        """Serialize question to JSON."""
        return json.dumps(
            {
                "question_id": question.question_id,
                "worker_id": question.worker_id,
                "task_id": question.task_id,
                "text": question.text,
                "context": question.context,
                "options": question.options,
                "status": question.status.value,
                "asked_at": question.asked_at.isoformat(),
                "answered_at": question.answered_at.isoformat()
                if question.answered_at
                else None,
                "answer": question.answer,
                "timeout_seconds": question.timeout_seconds,
            },
            indent=2,
        )

    def _serialize_answer(self, answer: Answer) -> str:
        """Serialize answer to JSON."""
        return json.dumps(
            {
                "question_id": answer.question_id,
                "worker_id": answer.worker_id,
                "text": answer.text,
                "answered_at": answer.answered_at.isoformat(),
                "answered_by": answer.answered_by,
            },
            indent=2,
        )

    def _load_question(self, filepath: Path) -> Question | None:
        """Load question from file."""
        try:
            data = json.loads(filepath.read_text())
            return Question(
                question_id=data["question_id"],
                worker_id=data["worker_id"],
                task_id=data["task_id"],
                text=data["text"],
                context=data.get("context", ""),
                options=data.get("options", []),
                status=QuestionStatus(data.get("status", "pending")),
                asked_at=datetime.fromisoformat(data["asked_at"]),
                answered_at=datetime.fromisoformat(data["answered_at"])
                if data.get("answered_at")
                else None,
                answer=data.get("answer"),
                timeout_seconds=data.get("timeout_seconds", 300),
            )
        except Exception:
            return None

    def _load_answer(self, filepath: Path) -> Answer | None:
        """Load answer from file."""
        try:
            data = json.loads(filepath.read_text())
            return Answer(
                question_id=data["question_id"],
                worker_id=data["worker_id"],
                text=data["text"],
                answered_at=datetime.fromisoformat(data["answered_at"]),
                answered_by=data.get("answered_by", "human"),
            )
        except Exception:
            return None

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write file atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp_path, path)
        except Exception:
            os.close(fd)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


def create_ipc_handler(project_root: Path | None = None) -> IPCHandler:
    """Factory function to create IPC handler."""
    return IPCHandler(project_root)


# CLI interface
def main() -> None:
    handler = IPCHandler()

    if len(sys.argv) < 2:
        print("Usage: ipc_handler.py <command> [args]")
        print("Commands: pending, ask, answer, cleanup")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "pending":
        questions = handler.get_pending_questions()
        if not questions:
            print("No pending questions")
        else:
            for q in questions:
                age = (datetime.now(timezone.utc) - q.asked_at).total_seconds()
                print(f"[{q.worker_id}] ({age:.0f}s ago)")
                print(f"  Task: {q.task_id}")
                print(f"  Question: {q.text}")
                if q.options:
                    print(f"  Options: {', '.join(q.options)}")
                print()

    elif cmd == "ask" and len(sys.argv) >= 4:
        worker_id = sys.argv[2]
        text = sys.argv[3]
        task_id = sys.argv[4] if len(sys.argv) > 4 else "unknown"
        question = handler.ask_question(worker_id, task_id, text)
        print(f"Question asked: {question.question_id}")

    elif cmd == "answer" and len(sys.argv) >= 4:
        worker_id = sys.argv[2]
        answer_text = sys.argv[3]
        success = handler.answer_question(worker_id, answer_text)
        print(f"Answer {'sent' if success else 'failed'}")

    elif cmd == "cleanup":
        count = handler.cleanup_expired()
        print(f"Cleaned up {count} expired IPC files")


if __name__ == "__main__":
    main()  # pragma: no cover - exercised via main() directly in tests
