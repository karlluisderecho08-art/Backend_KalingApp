from rest_framework import serializers

from articles.models import Article

from .citations import cited_articles
from .models import ChatMessage


class ChatMessageSerializer(serializers.ModelSerializer):
    """
    `sources` lists the Knowledge Hub articles Kali's "Source:" line names,
    as [{"article_id", "title"}], so the app can turn the citation into a
    link that opens the article. Worked out when the message is read, from
    the articles as they are now -- so older replies in her history get
    links too, and a citation of an article since deleted simply stops being
    one. Always empty for her own messages and for system notices.
    """

    sources = serializers.SerializerMethodField()

    class Meta:
        model = ChatMessage
        fields = ["id", "text", "is_user", "is_system_notice", "created_at", "sources"]

    def _articles(self):
        # Loaded once per response, not once per message: the history list
        # shares one context across all its items.
        if "_linkable_articles" not in self.context:
            self.context["_linkable_articles"] = list(Article.objects.only("id", "title", "content"))
        return self.context["_linkable_articles"]

    def get_sources(self, obj) -> list:
        if obj.is_user or obj.is_system_notice or "source" not in obj.text.lower():
            return []
        return [{"article_id": a.id, "title": a.title} for a in cited_articles(obj.text, self._articles())]


class SendMessageSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=2000)
