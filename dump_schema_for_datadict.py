"""
KalingApp — Data Dictionary field dump
--------------------------------------
Run this from the backend project root (where manage.py lives):

    python manage.py shell -c "exec(open('dump_schema_for_datadict.py', encoding='utf-8').read())"

(Piping it in with `manage.py shell < dump_schema_for_datadict.py` only
works on macOS/Linux: on Windows Django can't detect piped input and
falls back to the line-by-line console, which stops at the first blank
line inside a block.)

It prints, for every model in the project apps, each field's:
  - name
  - Django field type
  - DB column type (as Django would emit for the current DB)
  - max_length (the value the model-reference images left blank)
  - null / blank
  - default
  - relationship (FK / OneToOne / etc.) + on_delete + target
  - choices (enum values), if any

Copy the whole output back to the team. This is the authoritative
source for the data dictionary — it resolves every "confirm with Karl"
flag on field lengths, types, and constraints.

Also included at the bottom: the booking state machine (stage names,
allowed status moves, final statuses, the response clock and the
mother's notifications), read from milkbank/models.py and
milkbank/transitions.py.

Against PostgreSQL (DATABASE_URL pointing at a database built by
`manage.py migrate`), db_col shows the exact column types production uses.
"""

from django.apps import apps

# Only our own apps — skip Django/3rd-party. A tuple, not a set, so the
# models always print in the same order and two runs can be diffed.
OUR_APPS = ("accounts", "articles", "directory", "milkbank",
            "notifications", "chat", "messaging", "core")


def field_report(model):
    print("=" * 70)
    print(f"MODEL: {model.__name__}   (table: {model._meta.db_table})")
    print("=" * 70)
    for f in model._meta.get_fields():
        # skip reverse relations (they aren't columns)
        if f.auto_created and not f.concrete:
            continue
        name = f.name
        ftype = type(f).__name__
        # DB column type as emitted for the active DB backend
        try:
            from django.db import connection
            col = f.db_type(connection) or "-"
        except Exception:
            col = "-"
        max_len = getattr(f, "max_length", None)
        null = getattr(f, "null", None)
        blank = getattr(f, "blank", None)
        default = getattr(f, "default", None)
        # default can be a sentinel (NOT_PROVIDED) or a callable
        from django.db.models.fields import NOT_PROVIDED
        if default is NOT_PROVIDED:
            default = "(none)"
        elif callable(default):
            default = f"{default.__name__}()"
        rel = ""
        if f.is_relation:
            target = f.related_model.__name__ if f.related_model else "?"
            on_delete = ""
            try:
                on_delete = getattr(f.remote_field, "on_delete", None)
                on_delete = getattr(on_delete, "_name_", str(on_delete))
            except Exception:
                pass
            rel = f"-> {target} ({ftype}, on_delete={on_delete})"
        choices = getattr(f, "choices", None)
        choices_str = ""
        if choices:
            choices_str = " | ".join(str(c[0]) for c in choices)

        print(f"  {name}")
        print(f"      type={ftype}  db_col={col}  max_length={max_len}")
        print(f"      null={null}  blank={blank}  default={default}")
        if rel:
            print(f"      relation: {rel}")
        if choices_str:
            print(f"      choices: {choices_str}")
    print()


for app_label in OUR_APPS:
    try:
        app_config = apps.get_app_config(app_label)
    except LookupError:
        print(f"!! app '{app_label}' not found — check the app label")
        continue
    for model in app_config.get_models():
        field_report(model)

# ----- Booking state machine -----
# Printed straight from the code (milkbank/models.py and transitions.py),
# so this section can never drift from what the backend enforces.
print("#" * 70)
print("# BOOKING STATE MACHINE")
print("#" * 70)
try:
    from milkbank.models import MilkBankRequest
    from milkbank import transitions
    for label, stages in (("DONOR", MilkBankRequest.DONOR_STAGES),
                          ("RECIPIENT", MilkBankRequest.RECIPIENT_STAGES)):
        print(f"{label} stages (current_stage_index -> name):")
        for index, name in enumerate(stages):
            print(f"  {index} = {name}")
        print()
    print("Allowed status transitions (transitions.ALLOWED_TRANSITIONS):")
    for status, targets in transitions.ALLOWED_TRANSITIONS.items():
        moves = ", ".join(sorted(str(t) for t in targets)) or "(final, no further moves)"
        print(f"  {str(status):20} -> {moves}")
    print()
    print("Final statuses (set closed_at):", ", ".join(sorted(str(s) for s in transitions.FINAL_STATUSES)))
    print("Statuses with an 8-business-hour response clock:",
          ", ".join(sorted(str(s) for s in transitions.STATUSES_WITH_SLA_CLOCK)))
    print()
    print("Notification sent to the mother on reaching each status (transitions.STATUS_NOTIFICATIONS):")
    for status, message in transitions.STATUS_NOTIFICATIONS.items():
        print(f"  {str(status):20} \"{message}\"")
    print()
    print("Every transition goes through transitions.apply_transition(), which also writes")
    print("the audit log; reaching 'completed' creates a TransactionRecord and moves the")
    print("facility's stock_level_ml and the mother's lifetime total by the recorded mL.")
    print("current_stage_index and current_sub_status are independent: a request can sit")
    print("at one stage while its status changes.")
except Exception as e:
    print(f"Could not read the booking state machine: {e}")
