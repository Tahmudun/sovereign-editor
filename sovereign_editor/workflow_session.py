"""Reversible in-memory action queue. All planning and persistence use Project."""
import copy
from .formats import require


class WorkflowSession:
    def __init__(self, project):
        self.project = project
        self.revision = project.doc['revision']
        self.actions = []
        self.undone = []
        self.plan = None
        self.preview = project

    def set_actions(self, actions, clear_redo=True):
        candidate = copy.deepcopy(actions)
        plan = self.project.plan_workflow(candidate) if candidate else None
        preview = self.project.area_preview_project(plan) if plan else self.project
        self.actions, self.plan, self.preview = candidate, plan, preview
        if clear_redo:
            self.undone = []

    def stage(self, action):
        self.set_actions(self.actions + [action])

    def replace(self, index, action):
        require(type(index) is int and 0 <= index < len(self.actions), 'Select an action')
        actions = copy.deepcopy(self.actions); actions[index] = action
        self.set_actions(actions)

    def remove(self, index):
        require(type(index) is int and 0 <= index < len(self.actions), 'Select an action')
        actions = copy.deepcopy(self.actions); del actions[index]
        self.set_actions(actions)

    def undo(self):
        require(bool(self.actions), 'No pending action to undo', 'NO_UNDO')
        action = self.actions[-1]
        self.set_actions(self.actions[:-1], clear_redo=False)
        self.undone.append(action)

    def redo(self):
        require(bool(self.undone), 'No pending action to redo', 'NO_REDO')
        action = self.undone[-1]
        self.set_actions(self.actions + [action], clear_redo=False)
        self.undone.pop()

    def apply(self):
        require(bool(self.actions), 'No pending actions')
        result = self.project.apply_workflow(self.revision, self.actions)
        self.__init__(self.project)
        return result
