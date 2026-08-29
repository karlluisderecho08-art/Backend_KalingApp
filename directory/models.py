from django.db import models


class SupportContact(models.Model):
    """
    An entry in the Contact Directory (Arugaan, Fabella Human Milk Bank).
    Distinct from milkbank.Facility -- this is informational-only, not
    bookable (see CODEBASE-1.md section 7 on the two unreconciled
    datasets). phone/address are blank until filled in via admin; the
    Kotlin client already falls back to "pending verification" copy when
    they're empty, so leaving them blank here is a safe, honest default
    rather than a placeholder we'd have to remember to replace.
    """

    name = models.CharField(max_length=255)
    description = models.TextField()
    phone = models.CharField(max_length=50, blank=True)
    address = models.CharField(max_length=500, blank=True)
    email = models.EmailField(blank=True)

    def __str__(self):
        return self.name
