class Scheduler:
    def __init__(self):
        self.queue = []

    def submit(self, run_id):
        if run_id not in self.queue:
            self.queue.append(run_id)
        return run_id
