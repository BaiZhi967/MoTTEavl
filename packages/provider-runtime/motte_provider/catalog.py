class ModelCatalog:
    def __init__(self):
        self._models = {}

    def register(self, profile):
        self._models[profile["id"]] = dict(profile)

    def get(self, model_id):
        return self._models.get(model_id)

    def list(self):
        return list(self._models.values())
