from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone


class Command(BaseCommand):
    help = (
        "Repairs the specific inconsistent migration history where "
        "admin.0001_initial is recorded before users.0001_initial."
    )

    def handle(self, *args, **options):
        recorder = MigrationRecorder(connection)
        applied = set(recorder.applied_migrations())

        admin_initial = ("admin", "0001_initial")
        users_initial = ("users", "0001_initial")

        if users_initial in applied:
            self.stdout.write(self.style.SUCCESS("users.0001_initial is already recorded. No repair needed."))
            return

        if admin_initial not in applied:
            self.stdout.write("admin.0001_initial is not applied. No migration history repair needed.")
            return

        existing_tables = set(connection.introspection.table_names())
        if "users_user" not in existing_tables:
            raise CommandError(
                "Cannot safely repair migration history: table 'users_user' does not exist. "
                "This database likely came from an older auth schema and needs a one-time manual reset "
                "or a data migration plan before deploy."
            )

        self.stdout.write(
            "Detected admin.0001_initial applied before users.0001_initial with users_user table present. "
            "Recording users.0001_initial so Django can continue migrating."
        )

        with transaction.atomic():
            recorder.migration_qs.create(app="users", name="0001_initial", applied=timezone.now())

        self.stdout.write(self.style.SUCCESS("Recorded users.0001_initial successfully."))
