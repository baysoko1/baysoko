import os
import subprocess
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Restore Render PostgreSQL database using pg_restore."

    def add_arguments(self, parser):
        parser.add_argument("backup_file", help="Path to .dump backup file")
        parser.add_argument(
            "--database-url",
            default=os.environ.get("DATABASE_URL", ""),
            help="Database URL (default: env DATABASE_URL)",
        )

    def handle(self, *args, **options):
        backup_file = options["backup_file"]
        db_url = options["database_url"]

        if not os.path.exists(backup_file):
            raise CommandError(f"Backup file not found: {backup_file}")

        if not db_url:
            raise CommandError("DATABASE_URL is not set and --database-url not provided.")

        cmd = [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            db_url,
            backup_file,
        ]

        self.stdout.write(f"Restoring database from {backup_file}")
        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError as exc:
            raise CommandError("pg_restore not found in PATH.") from exc
        except subprocess.CalledProcessError as exc:
            raise CommandError(f"pg_restore failed with exit code {exc.returncode}") from exc

        self.stdout.write(self.style.SUCCESS("Restore complete."))
