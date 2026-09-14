class ReplaySource:
    def __init__(self, outputs):
        self.outputs = outputs

    def get(self, case_id):
        return self.outputs[case_id]
