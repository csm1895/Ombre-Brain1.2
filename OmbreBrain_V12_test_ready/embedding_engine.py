class EmbeddingEngine:
    def __init__(self, *args, **kwargs):
        pass

    def embed_text(self, text):
        return [0.0] * 8

    def embed(self, text):
        return self.embed_text(text)

    def similarity(self, a, b):
        return 0.0
