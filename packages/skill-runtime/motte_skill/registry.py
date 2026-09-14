class SkillRegistry:
    def __init__(self):
        self._skills = {}

    def register(self, skill):
        self._skills[skill.name] = skill

    def get(self, name):
        return self._skills.get(name)
