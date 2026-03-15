import os
import subprocess
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Backup Render PostgreSQL database using pg_dump."

    def add_arguments(self, parser):
        parser.add_argument(
            "--outdir",
            default="backups",
            help="Directory to write backups to (default: backups)",
        )
        parser.add_argument(
            "--database-url",
            default=os.environ.get("DATABASE_URL", ""),
            help="Database URL (default: env DATABASE_URL)",
        )

    def handle(self, *args, **options):
        outdir = options["outdir"]
        db_url = options["database_url"]

        if not db_url:
            raise CommandError("DATABASE_URL is not set and --database-url not provided.")

        os.makedirs(outdir, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        outfile = os.path.join(outdir, f"baysoko_backup_{ts}.dump")

        cmd = [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--file",
            outfile,
            db_url,
        ]

        self.stdout.write(f"Backing up database to {outfile}")
        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError as exc:
            raise CommandError("pg_dump not found in PATH.") from exc
        except subprocess.CalledProcessError as exc:
            raise CommandError(f"pg_dump failed with exit code {exc.returncode}") from exc

        self.stdout.write(self.style.SUCCESS("Backup complete."))
