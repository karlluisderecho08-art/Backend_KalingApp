"""
KalingApp — Data Dictionary field dump
--------------------------------------
Run this from the backend project root (where manage.py lives):

    python manage.py shell < dump_schema_for_datadict.py

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

Also included at the bottom: a dump of the booking state-machine
mapping IF a STAGES / STAGE_NAMES constant exists on MilkBankRequest
or in transitions.py — otherwise it prints a NOTE asking Karl to
paste the stage_index -> stage-name mapping manually.
"""

from django.apps import apps

# Only our own apps — skip Django/3rd-party
OUR_APPS = {"accounts", "articles", "directory", "milkbank",
            "notifications", "chat", "core"}


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

# ----- Booking state machine mapping -----
print("#" * 70)
print("# BOOKING STATE MACHINE — stage_index -> stage name")
print("#" * 70)
try:
    from milkbank.models import MilkBankRequest
    found = False
    for attr in ("STAGES", "STAGE_NAMES", "STAGE_LABELS", "STAGE_MAP"):
        if hasattr(MilkBankRequest, attr):
            print(f"{attr} = {getattr(MilkBankRequest, attr)}")
            found = True
    if not found:
        print("NOTE: No STAGES/STAGE_NAMES constant found on MilkBankRequest.")
        print("      The stage_index -> stage-name mapping likely lives in")
        print("      milkbank/transitions.py. Karl: please paste that mapping,")
        print("      e.g. 0=Status, 1=Booking, 2=Screening, 3=Analysis, 4=Results,")
        print("      AND which current_sub_status values are valid at each stage.")
except Exception as e:
    print(f"Could not import MilkBankRequest: {e}")
