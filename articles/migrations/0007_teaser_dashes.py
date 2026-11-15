"""
Two seeded articles' teasers showed a double hyphen ("--") where a dash was
meant. seed_articles only creates an article that doesn't exist yet, so its
corrected text never reaches the articles already in the database -- this
updates them.

Only a teaser that is still exactly the seeded text is changed: one an admin
has since edited in the admin portal is left as they wrote it.
"""

from django.db import migrations

DASH = "—"

TEASERS = [
    (
        "How to Breastfeed Correctly: Positioning and Latch",
        "The fundamentals of a comfortable, effective latch -- how to position your baby and recognize "
        "the signs that feeding is going well.",
    ),
    (
        "5 Common Breastfeeding Problems and How to Handle Them",
        "From latch trouble to low supply, engorgement, cracked nipples, and blocked ducts -- practical "
        "guidance for the most common breastfeeding challenges.",
    ),
]


def _swap(apps, frm, to):
    Article = apps.get_model("articles", "Article")
    for title, old in TEASERS:
        new = old.replace(" -- ", f" {DASH} ")
        before, after = (old, new) if frm == "old" else (new, old)
        Article.objects.filter(title=title, teaser=before).update(teaser=after)


def forwards(apps, schema_editor):
    _swap(apps, "old", "new")


def backwards(apps, schema_editor):
    _swap(apps, "new", "old")


class Migration(migrations.Migration):
    dependencies = [
        ("articles", "0006_article_created_at"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
