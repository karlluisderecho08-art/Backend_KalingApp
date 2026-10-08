"""
One-off repair of article bodies whose bold/italic markers were written across a
line break by the old admin editor (e.g. "**1. Getting a good latch\\n**"), which
the app shows as literal asterisks. New saves are repaired by
AdminArticleSerializer.validate_content; this fixes what is already stored.

Uses the same normalize_markers() so the two can never disagree. It is a no-op
for any article that is already well-formed, so it is safe on every database.
Not reversible in any meaningful sense -- the broken form was never intended.
"""

from django.db import migrations

from articles.formatting import normalize_markers


def repair(apps, schema_editor):
    Article = apps.get_model("articles", "Article")
    for article in Article.objects.filter(content__contains="*").only("id", "content"):
        fixed = normalize_markers(article.content)
        if fixed != article.content:
            Article.objects.filter(pk=article.pk).update(content=fixed)


class Migration(migrations.Migration):

    dependencies = [
        ("articles", "0002_resourcelink"),
    ]

    operations = [
        migrations.RunPython(repair, migrations.RunPython.noop),
    ]
