"""
Tests for L{eliot._action}.
"""

from __future__ import unicode_literals

from unittest import TestCase
from threading import Thread
from warnings import catch_warnings, simplefilter

from hypothesis import assume, given, Settings
from hypothesis.strategies import (
    integers,
    lists,
    just,
    text,
)

from pyrsistent import pvector, v

import testtools
from testtools.matchers import MatchesStructure

from .._action import (
    Action, _ExecutionContext, currentAction, startTask, startAction,
    ACTION_STATUS_FIELD, ACTION_TYPE_FIELD, FAILED_STATUS, STARTED_STATUS,
    SUCCEEDED_STATUS, DuplicateChild, InvalidStartMessage, InvalidStatus,
    TaskLevel, WrittenAction, WrongActionType, WrongTask, WrongTaskLevel)
from .._message import (
    EXCEPTION_FIELD, REASON_FIELD, TASK_LEVEL_FIELD, TASK_UUID_FIELD,
    WrittenMessage)
from .._output import MemoryLogger
from .._validation import ActionType, Field, _ActionSerializers
from ..testing import assertContainsFields
from .. import _action, add_destination, remove_destination

from .strategies import (
    MESSAGE_DICTS,
    START_ACTION_MESSAGE_DICTS,
    START_ACTION_MESSAGES,
    TASK_LEVEL_INDEXES,
    TASK_LEVEL_LISTS,
    WRITTEN_ACTIONS,
    WRITTEN_MESSAGES,
    _reparent_action,
    sibling_task_level,
    union,
)


class ExecutionContextTests(TestCase):
    """
    Tests for L{_ExecutionContext}.
    """
    def test_nothingPushed(self):
        """
        If no action has been pushed, L{_ExecutionContext.current} returns
        C{None}.
        """
        ctx = _ExecutionContext()
        self.assertIs(ctx.current(), None)


    def test_pushSingle(self):
        """
        L{_ExecutionContext.current} returns the action passed to
        L{_ExecutionContext.push} (assuming no pops).
        """
        ctx = _ExecutionContext()
        a = object()
        ctx.push(a)
        self.assertIs(ctx.current(), a)


    def test_pushMultiple(self):
        """
        L{_ExecutionContext.current} returns the action passed to the last
        call to L{_ExecutionContext.push} (assuming no pops).
        """
        ctx = _ExecutionContext()
        a = object()
        b = object()
        ctx.push(a)
        ctx.push(b)
        self.assertIs(ctx.current(), b)


    def test_multipleCurrent(self):
        """
        Multiple calls to L{_ExecutionContext.current} return the same result.
        """
        ctx = _ExecutionContext()
        a = object()
        ctx.push(a)
        ctx.current()
        self.assertIs(ctx.current(), a)


    def test_popSingle(self):
        """
        L{_ExecutionContext.pop} cancels a L{_ExecutionContext.push}, leading
        to an empty context.
        """
        ctx = _ExecutionContext()
        a = object()
        ctx.push(a)
        ctx.pop()
        self.assertIs(ctx.current(), None)


    def test_popMultiple(self):
        """
        L{_ExecutionContext.pop} cancels the last L{_ExecutionContext.push},
        resulting in current context being whatever was pushed before that.
        """
        ctx = _ExecutionContext()
        a = object()
        b = object()
        ctx.push(a)
        ctx.push(b)
        ctx.pop()
        self.assertIs(ctx.current(), a)


    def test_threadStart(self):
        """
        Each thread starts with an empty execution context.
        """
        ctx = _ExecutionContext()
        first = object()
        ctx.push(first)

        valuesInThread = []
        def inthread():
            valuesInThread.append(ctx.current())
        thread = Thread(target=inthread)
        thread.start()
        thread.join()
        self.assertEqual(valuesInThread, [None])


    def test_threadSafety(self):
        """
        Each thread gets its own execution context.
        """
        ctx = _ExecutionContext()
        first = object()
        ctx.push(first)

        second = object()
        valuesInThread = []
        def inthread():
            ctx.push(second)
            valuesInThread.append(ctx.current())
        thread = Thread(target=inthread)
        thread.start()
        thread.join()
        # Neither thread was affected by the other:
        self.assertEqual(valuesInThread, [second])
        self.assertIs(ctx.current(), first)


    def test_globalInstance(self):
        """
        A global L{_ExecutionContext} is exposed in the L{eliot._action}
        module.
        """
        self.assertIsInstance(_action._context, _ExecutionContext)
        self.assertEqual(_action.currentAction, _action._context.current)



class ActionTests(TestCase):
    """
    Tests for L{Action}.
    """
    def test_start(self):
        """
        L{Action._start} logs an C{action_status="started"} message.
        """
        logger = MemoryLogger()
        action = Action(logger, "unique", TaskLevel(level=[]), "sys:thename")
        action._start({"key": "value"})
        assertContainsFields(self, logger.messages[0],
                             {"task_uuid": "unique",
                              "task_level": [1],
                              "action_type": "sys:thename",
                              "action_status": "started",
                              "key": "value"})


    def test_startMessageSerialization(self):
        """
        The start message logged by L{Action._start} is created with the
        appropriate start message L{eliot._validation._MessageSerializer}.
        """
        serializers = ActionType("sys:thename",
                                 [Field("key", lambda x: x, "")],
                                 [], "")._serializers
        class Logger(list):
            def write(self, msg, serializer):
                self.append(serializer)
        logger = Logger()
        action = Action(logger, "unique", TaskLevel(level=[]), "sys:thename",
                        serializers)
        action._start({"key": "value"})
        self.assertIs(logger[0], serializers.start)


    def test_child(self):
        """
        L{Action.child} returns a new L{Action} with the given logger, system
        and name, and a task_uuid taken from the parent L{Action}.
        """
        logger = MemoryLogger()
        action = Action(logger, "unique", TaskLevel(level=[]), "sys:thename")
        logger2 = MemoryLogger()
        child = action.child(logger2, "newsystem:newname")
        self.assertEqual([child._logger, child._identification,
                          child._task_level],
                         [logger2, {"task_uuid": "unique",
                          "action_type": "newsystem:newname"},
                          TaskLevel(level=[1])])


    def test_childLevel(self):
        """
        Each call to L{Action.child} increments the new sub-level set on the
        child.
        """
        logger = MemoryLogger()
        action = Action(logger, "unique", TaskLevel(level=[]), "sys:thename")
        child1 = action.child(logger, "newsystem:newname")
        child2 = action.child(logger, "newsystem:newname")
        child1_1 = child1.child(logger, "newsystem:other")
        self.assertEqual(child1._task_level, TaskLevel(level=[1]))
        self.assertEqual(child2._task_level, TaskLevel(level=[2]))
        self.assertEqual(child1_1._task_level, TaskLevel(level=[1, 1]))


    def test_childSerializers(self):
        """
        L{Action.child} returns a new L{Action} with the serializers passed to
        it, rather than the parent's.
        """
        logger = MemoryLogger()
        serializers = object()
        action = Action(logger, "unique", TaskLevel(level=[]), "sys:thename",
                        serializers)
        childSerializers = object()
        child = action.child(logger, "newsystem:newname", childSerializers)
        self.assertIs(child._serializers, childSerializers)


    def test_run(self):
        """
        L{Action.run} runs the given function with given arguments, returning
        its result.
        """
        action = Action(None, "", TaskLevel(level=[]), "")
        def f(*args, **kwargs):
            return args, kwargs
        result = action.run(f, 1, 2, x=3)
        self.assertEqual(result, ((1, 2), {"x": 3}))


    def test_runContext(self):
        """
        L{Action.run} runs the given function with the action set as the
        current action.
        """
        result = []
        action = Action(None, "", TaskLevel(level=[]), "")
        action.run(lambda: result.append(currentAction()))
        self.assertEqual(result, [action])


    def test_runContextUnsetOnReturn(self):
        """
        L{Action.run} unsets the action once the given function returns.
        """
        action = Action(None, "", TaskLevel(level=[]), "")
        action.run(lambda: None)
        self.assertIs(currentAction(), None)


    def test_runContextUnsetOnRaise(self):
        """
        L{Action.run} unsets the action once the given function raises an
        exception.
        """
        action = Action(None, "", TaskLevel(level=[]), "")
        self.assertRaises(ZeroDivisionError, action.run, lambda: 1/0)
        self.assertIs(currentAction(), None)


    def test_withSetsContext(self):
        """
        L{Action.__enter__} sets the action as the current action.
        """
        action = Action(MemoryLogger(), "", TaskLevel(level=[]), "")
        with action:
            self.assertIs(currentAction(), action)


    def test_withUnsetOnReturn(self):
        """
        L{Action.__exit__} unsets the action on successful block finish.
        """
        action = Action(MemoryLogger(), "", TaskLevel(level=[]), "")
        with action:
            pass
        self.assertIs(currentAction(), None)


    def test_withUnsetOnRaise(self):
        """
        L{Action.__exit__} unsets the action if the block raises an exception.
        """
        action = Action(MemoryLogger(), "", TaskLevel(level=[]), "")
        try:
            with action:
                1/0
        except ZeroDivisionError:
            pass
        else:
            self.fail("no exception")
        self.assertIs(currentAction(), None)


    def test_withContextSetsContext(self):
        """
        L{Action.context().__enter__} sets the action as the current action.
        """
        action = Action(MemoryLogger(), "", TaskLevel(level=[]), "")
        with action.context():
            self.assertIs(currentAction(), action)


    def test_withContextUnsetOnReturn(self):
        """
        L{Action.context().__exit__} unsets the action on successful block
        finish.
        """
        action = Action(MemoryLogger(), "", TaskLevel(level=[]), "")
        with action.context():
            pass
        self.assertIs(currentAction(), None)


    def test_withContextNoLogging(self):
        """
        L{Action.context().__exit__} does not log any messages.
        """
        logger = MemoryLogger()
        action = Action(logger, "", TaskLevel(level=[]), "")
        with action.context():
            pass
        self.assertFalse(logger.messages)


    def test_withContextUnsetOnRaise(self):
        """
        L{Action.conext().__exit__} unsets the action if the block raises an
        exception.
        """
        action = Action(MemoryLogger(), "", TaskLevel(level=[]), "")
        try:
            with action.context():
                1/0
        except ZeroDivisionError:
            pass
        else:
            self.fail("no exception")
        self.assertIs(currentAction(), None)


    def test_finish(self):
        """
        L{Action.finish} with no exception logs an C{action_status="succeeded"}
        message.
        """
        logger = MemoryLogger()
        action = Action(logger, "unique", TaskLevel(level=[]), "sys:thename")
        action.finish()
        assertContainsFields(self, logger.messages[0],
                             {"task_uuid": "unique",
                              "task_level": [1],
                              "action_type": "sys:thename",
                              "action_status": "succeeded"})


    def test_successfulFinishSerializer(self):
        """
        L{Action.finish} with no exception passes the success
        L{eliot._validation._MessageSerializer} to the message it creates.
        """
        serializers = ActionType("sys:thename",
                                 [],
                                 [Field("key", lambda x: x, "")],
                                 "")._serializers
        class Logger(list):
            def write(self, msg, serializer):
                self.append(serializer)
        logger = Logger()
        action = Action(logger, "unique", TaskLevel(level=[]), "sys:thename", serializers)
        action.finish()
        self.assertIs(logger[0], serializers.success)


    def test_failureFinishSerializer(self):
        """
        L{Action.finish} with an exception passes the failure
        L{eliot._validation._MessageSerializer} to the message it creates.
        """
        serializers = ActionType("sys:thename", [],
                                 [Field("key", lambda x: x, "")],
                                 "")._serializers
        class Logger(list):
            def write(self, msg, serializer):
                self.append(serializer)
        logger = Logger()
        action = Action(logger, "unique", TaskLevel(level=[]), "sys:thename", serializers)
        action.finish(Exception())
        self.assertIs(logger[0], serializers.failure)


    def test_startFieldsNotInFinish(self):
        """
        L{Action.finish} logs a message without the fields from
        L{Action._start}.
        """
        logger = MemoryLogger()
        action = Action(logger, "unique", TaskLevel(level=[]), "sys:thename")
        action._start({"key": "value"})
        action.finish()
        self.assertNotIn("key", logger.messages[1])


    def test_finishWithBadException(self):
        """
        L{Action.finish} still logs a message if the given exception raises
        another exception when called with C{unicode()}.
        """
        logger = MemoryLogger()
        action = Action(logger, "unique", TaskLevel(level=[]), "sys:thename")
        class BadException(Exception):
            def __str__(self):
                raise TypeError()
        action.finish(BadException())
        self.assertEqual(logger.messages[0]["reason"],
                         "eliot: unknown, unicode() raised exception")


    def test_withLogsSuccessfulFinishMessage(self):
        """
        L{Action.__exit__} logs an action finish message on a successful block
        finish.
        """
        logger = MemoryLogger()
        action = Action(logger, "uuid", TaskLevel(level=[1]), "sys:me")
        with action:
            pass
        # Start message is only created if we use the action()/task() utility
        # functions, the intended public APIs.
        self.assertEqual(len(logger.messages), 1)
        assertContainsFields(self, logger.messages[0],
                             {"task_uuid": "uuid",
                              "task_level": [1, 1],
                              "action_type": "sys:me",
                              "action_status": "succeeded"})


    def test_withLogsExceptionMessage(self):
        """
        L{Action.__exit__} logs an action finish message on an exception
        raised from the block.
        """
        logger = MemoryLogger()
        action = Action(logger, "uuid", TaskLevel(level=[1]), "sys:me")
        exception = RuntimeError("because")

        try:
            with action:
                raise exception
        except RuntimeError:
            pass
        else:
            self.fail("no exception")

        self.assertEqual(len(logger.messages), 1)
        assertContainsFields(self, logger.messages[0],
                             {"task_uuid": "uuid",
                              "task_level": [1, 1],
                              "action_type": "sys:me",
                              "action_status": "failed",
                              "reason": "because",
                              "exception": "%s.RuntimeError" % (
                                  RuntimeError.__module__,)})


    def test_withReturnValue(self):
        """
        L{Action.__enter__} returns the action itself.
        """
        logger = MemoryLogger()
        action = Action(logger, "uuid", TaskLevel(level=[1]), "sys:me")
        with action as act:
            self.assertIs(action, act)


    def test_addSuccessFields(self):
        """
        On a successful finish, L{Action.__exit__} adds fields from
        L{Action.addSuccessFields} to the result message.
        """
        logger = MemoryLogger()
        action = Action(logger, "uuid", TaskLevel(level=[1]), "sys:me")
        with action as act:
            act.addSuccessFields(x=1, y=2)
            act.addSuccessFields(z=3)
        assertContainsFields(self, logger.messages[0],
                             {"x": 1, "y": 2, "z": 3})


    def test_nextTaskLevel(self):
        """
        Each call to L{Action._nextTaskLevel()} increments a counter.
        """
        action = Action(MemoryLogger(), "uuid", TaskLevel(level=[1]), "sys:me")
        self.assertEqual([action._nextTaskLevel() for i in range(5)],
                         [TaskLevel(level=level) for level in
                          ([1, 1], [1, 2], [1, 3], [1, 4], [1, 5])])


    def test_multipleFinishCalls(self):
        """
        If L{Action.finish} is called, subsequent calls to L{Action.finish}
        have no effect.
        """
        logger = MemoryLogger()
        action = Action(logger, "uuid", TaskLevel(level=[1]), "sys:me")
        with action as act:
            act.finish()
            act.finish(Exception())
            act.finish()
        # Only initial finish message is logged:
        self.assertEqual(len(logger.messages), 1)


    def test_stringActionCompatibility(self):
        """
        L{Action} can be initialized with a string version of a L{TaskLevel},
        for backwards compatibility.
        """
        logger = MemoryLogger()
        action = Action(logger, "uuid", "/1/2/", "sys:me")
        self.assertEqual(action._task_level, TaskLevel(level=[1, 2]))


    def test_stringActionCompatibilityWarning(self):
        """
        Calling L{Action} with a string results in a L{DeprecationWarning}
        """
        logger = MemoryLogger()
        with catch_warnings(record=True) as warnings:
            simplefilter("always")  # Catch all warnings
            Action(logger, "uuid", "/1/2/", "sys:me")
            self.assertEqual(warnings[-1].category, DeprecationWarning)



class StartActionAndTaskTests(TestCase):
    """
    Tests for L{startAction} and L{startTask}.
    """
    def test_startTaskNewAction(self):
        """
        L{startTask} creates a new top-level L{Action}.
        """
        logger = MemoryLogger()
        action = startTask(logger, "sys:do")
        self.assertIsInstance(action, Action)
        self.assertEqual(action._task_level, TaskLevel(level=[]))


    def test_startTaskSerializers(self):
        """
        If serializers are passed to L{startTask} they are attached to the
        resulting L{Action}.
        """
        logger = MemoryLogger()
        serializers = _ActionSerializers(None, None, None)
        action = startTask(logger, "sys:do", serializers)
        self.assertIs(action._serializers, serializers)


    def test_startActionSerializers(self):
        """
        If serializers are passed to L{startAction} they are attached to the
        resulting L{Action}.
        """
        logger = MemoryLogger()
        serializers = _ActionSerializers(None, None, None)
        action = startAction(logger, "sys:do", serializers)
        self.assertIs(action._serializers, serializers)


    def test_startTaskNewUUID(self):
        """
        L{startTask} creates an L{Action} with its own C{task_uuid}.
        """
        logger = MemoryLogger()
        action = startTask(logger, "sys:do")
        action2 = startTask(logger, "sys:do")
        self.assertNotEqual(action._identification["task_uuid"],
                            action2._identification["task_uuid"])


    def test_startTaskLogsStart(self):
        """
        L{startTask} logs a start message for the newly created L{Action}.
        """
        logger = MemoryLogger()
        action = startTask(logger, "sys:do", key="value")
        assertContainsFields(self, logger.messages[0],
                             {"task_uuid": action._identification["task_uuid"],
                              "task_level": [1],
                              "action_type": "sys:do",
                              "action_status": "started",
                              "key": "value"})


    def test_startActionNoParent(self):
        """
        L{startAction} when C{currentAction()} is C{None} creates a top-level
        L{Action}.
        """
        logger = MemoryLogger()
        action = startAction(logger, "sys:do")
        self.assertIsInstance(action, Action)
        self.assertEqual(action._task_level, TaskLevel(level=[]))


    def test_startActionNoParentLogStart(self):
        """
        L{startAction} when C{currentAction()} is C{None} logs a start
        message.
        """
        logger = MemoryLogger()
        action = startAction(logger, "sys:do", key="value")
        assertContainsFields(self, logger.messages[0],
                             {"task_uuid": action._identification["task_uuid"],
                              "task_level": [1],
                              "action_type": "sys:do",
                              "action_status": "started",
                              "key": "value"})


    def test_startActionWithParent(self):
        """
        L{startAction} uses the C{currentAction()} as parent for a new
        L{Action}.
        """
        logger = MemoryLogger()
        parent = Action(logger, "uuid", TaskLevel(level=[2]), "other:thing")
        with parent:
            action = startAction(logger, "sys:do")
            self.assertIsInstance(action, Action)
            self.assertEqual(action._identification["task_uuid"], "uuid")
            self.assertEqual(action._task_level, TaskLevel(level=[2, 1]))


    def test_startActionWithParentLogStart(self):
        """
        L{startAction} when C{currentAction()} is an L{Action} logs a start
        message.
        """
        logger = MemoryLogger()
        parent = Action(logger, "uuid", TaskLevel(level=[]), "other:thing")
        with parent:
            startAction(logger, "sys:do", key="value")
            assertContainsFields(self, logger.messages[0],
                                 {"task_uuid": "uuid",
                                  "task_level": [1, 1],
                                  "action_type": "sys:do",
                                  "action_status": "started",
                                  "key": "value"})


    def test_startTaskNoLogger(self):
        """
        When no logger is given L{startTask} logs to the default ``Logger``.
        """
        messages = []
        add_destination(messages.append)
        self.addCleanup(remove_destination, messages.append)
        action = startTask(action_type="sys:do", key="value")
        assertContainsFields(self, messages[0],
                             {"task_uuid": action._identification["task_uuid"],
                              "task_level": [1],
                              "action_type": "sys:do",
                              "action_status": "started",
                              "key": "value"})


    def test_startActionNoLogger(self):
        """
        When no logger is given L{startAction} logs to the default ``Logger``.
        """
        messages = []
        add_destination(messages.append)
        self.addCleanup(remove_destination, messages.append)
        action = startAction(action_type="sys:do", key="value")
        assertContainsFields(self, messages[0],
                             {"task_uuid": action._identification["task_uuid"],
                              "task_level": [1],
                              "action_type": "sys:do",
                              "action_status": "started",
                              "key": "value"})


class PEP8Tests(TestCase):
    """
    Tests for PEP 8 method compatibility.
    """
    def test_add_success_fields(self):
        """
        L{Action.addSuccessFields} is the same as L{Action.add_success_fields}.
        """
        self.assertEqual(Action.addSuccessFields, Action.add_success_fields)


    def test_serialize_task_id(self):
        """
        L{Action.serialize_task_id} is the same as L{Action.serializeTaskId}.
        """
        self.assertEqual(Action.serialize_task_id, Action.serializeTaskId)


    def test_continue_task(self):
        """
        L{Action.continue_task} is the same as L{Action.continueTask}.
        """
        self.assertEqual(Action.continue_task, Action.continueTask)


class SerializationTests(TestCase):
    """
    Tests for L{Action} serialization and deserialization.
    """
    def test_serializeTaskId(self):
        """
        L{Action.serializeTaskId} result is composed of the task UUID and an
        incremented task level.
        """
        action = Action(None, "uniq123", TaskLevel(level=[1, 2]), "mytype")
        self.assertEqual([action._nextTaskLevel(),
                          action.serializeTaskId(),
                          action._nextTaskLevel()],
                         [TaskLevel(level=[1, 2, 1]),
                          b"uniq123@/1/2/2",
                          TaskLevel(level=[1, 2, 3])])


    def test_continueTaskReturnsAction(self):
        """
        L{Action.continueTask} returns an L{Action} whose C{task_level} and
        C{task_uuid} are derived from those in the given serialized task
        identifier.
        """
        originalAction = Action(None, "uniq456", TaskLevel(level=[3, 4]),
                                "mytype")
        taskId = originalAction.serializeTaskId()

        newAction = Action.continueTask(MemoryLogger(), taskId)
        self.assertEqual([newAction.__class__, newAction._identification,
                          newAction._task_level],
                         [Action, {"task_uuid": "uniq456",
                                   "action_type": "eliot:remote_task"},
                          TaskLevel(level=[3, 4, 1])])

    def test_continueTaskStartsAction(self):
        """
        L{Action.continueTask} starts the L{Action} it creates.
        """
        originalAction = Action(None, "uniq456", TaskLevel(level=[3, 4]),
                                "mytype")
        taskId = originalAction.serializeTaskId()
        logger = MemoryLogger()

        Action.continueTask(logger, taskId)
        assertContainsFields(self, logger.messages[0],
                             {"task_uuid": "uniq456",
                              "task_level": [3, 4, 1, 1],
                              "action_type": "eliot:remote_task",
                              "action_status": "started"})


    def test_continueTaskNoLogger(self):
        """
        L{Action.continueTask} can be called without a logger.
        """
        originalAction = Action(None, "uniq456", TaskLevel(level=[3, 4]),
                                "mytype")
        taskId = originalAction.serializeTaskId()

        messages = []
        add_destination(messages.append)
        self.addCleanup(remove_destination, messages.append)
        Action.continueTask(task_id=taskId)
        assertContainsFields(self, messages[0],
                             {"task_uuid": "uniq456",
                              "task_level": [3, 4, 1, 1],
                              "action_type": "eliot:remote_task",
                              "action_status": "started"})

    def test_continueTaskRequiredTaskId(self):
        """
        L{Action.continue_task} requires a C{task_id} to be passed in.
        """
        self.assertRaises(RuntimeError, Action.continueTask)


class TaskLevelTests(TestCase):
    """
    Tests for L{TaskLevel}.
    """

    def assert_fully_less_than(self, x, y):
        """
        Assert that x < y according to all the comparison operators.
        """
        self.assertTrue(all([
            # lt
            x < y,
            not y < x,
            # le
            x <= y,
            not y <= x,
            # gt
            y > x,
            not x > y,
            # ge
            y >= x,
            not x >= y,
            # eq
            not x == y,
            not y == x,
            # ne
            x != y,
            y != x,
        ]))


    @given(lists(TASK_LEVEL_INDEXES))
    def test_parent_of_child(self, base_task_level):
        """
        L{TaskLevel.child} returns the first child of the task.
        """
        base_task = TaskLevel(level=base_task_level)
        child_task = base_task.child()
        self.assertEqual(base_task, child_task.parent())


    @given(TASK_LEVEL_LISTS)
    def test_child_greater_than_parent(self, task_level):
        """
        L{TaskLevel.child} returns a child that is greater than its parent.
        """
        task = TaskLevel(level=task_level)
        self.assert_fully_less_than(task, task.child())


    @given(TASK_LEVEL_LISTS)
    def test_next_sibling_greater(self, task_level):
        """
        L{TaskLevel.next_sibling} returns a greater task level.
        """
        task = TaskLevel(level=task_level)
        self.assert_fully_less_than(task, task.next_sibling())


    @given(TASK_LEVEL_LISTS)
    def test_next_sibling(self, task_level):
        """
        L{TaskLevel.next_sibling} returns the next sibling of a task.
        """
        task = TaskLevel(level=task_level)
        sibling = task.next_sibling()
        self.assertEqual(
            sibling, TaskLevel(level=task_level[:-1] + [task_level[-1] + 1]))


    def test_parent_of_root(self):
        """
        L{TaskLevel.parent} of the root task level is C{None}.
        """
        self.assertIs(TaskLevel(level=[]).parent(), None)


    def test_toString(self):
        """
        L{TaskLevel.toString} serializes the object to a Unicode string.
        """
        root = TaskLevel(level=[])
        child2_1 = root.child().next_sibling().child()
        self.assertEqual([root.toString(), child2_1.toString()],
                         ["/", "/2/1"])


    def test_fromString(self):
        """
        L{TaskLevel.fromString} deserializes the output of
        L{TaskLevel.toString}.
        """
        self.assertEqual([TaskLevel.fromString("/"), TaskLevel.fromString("/2/1")],
                         [TaskLevel(level=[]), TaskLevel(level=[2, 1])])


    def test_from_string(self):
        """
        L{TaskLevel.from_string} is the same as as L{TaskLevel.fromString}.
        """
        self.assertEqual(TaskLevel.from_string, TaskLevel.fromString)


    def test_to_string(self):
        """
        L{TaskLevel.to_string} is the same as as L{TaskLevel.toString}.
        """
        self.assertEqual(TaskLevel.to_string, TaskLevel.toString)


class WrittenActionTests(testtools.TestCase):
    """
    Tests for L{WrittenAction}.
    """

    @given(START_ACTION_MESSAGES)
    def test_from_single_message(self, message):
        """
        A L{WrittenAction} can be constructed from a single "start" message. Such
        an action inherits the C{action_type} of the start message, has no
        C{end_time}, and has a C{status} of C{STARTED_STATUS}.
        """
        action = WrittenAction.from_messages(message)
        self.assertThat(
            action, MatchesStructure.byEquality(
                status=STARTED_STATUS,
                action_type=message.contents[ACTION_TYPE_FIELD],
                task_uuid=message.task_uuid,
                task_level=message.task_level.parent(),
                start_time=message.timestamp,
                children=pvector([]),
                end_time=None,
                reason=None,
                exception=None,
            ))

    @given(START_ACTION_MESSAGES, MESSAGE_DICTS, integers(min_value=2))
    def test_different_task_uuid(self, start_message, end_message_dict, n):
        """
        By definition, an action is either a top-level task or takes place within
        such a task. If we try to assemble actions from messages with
        differing task UUIDs, we raise an error.
        """
        assume(start_message.task_uuid != end_message_dict['task_uuid'])
        end_message = WrittenMessage.from_dict(
            union(end_message_dict, {
                ACTION_STATUS_FIELD: SUCCEEDED_STATUS,
                TASK_LEVEL_FIELD: sibling_task_level(start_message, n),
            }))
        self.assertRaises(
            WrongTask,
            WrittenAction.from_messages, start_message, end_message=end_message)

    @given(MESSAGE_DICTS)
    def test_invalid_start_message_missing_status(self, message_dict):
        """
        A start message must have an C{ACTION_STATUS_FIELD} with the value
        C{STARTED_STATUS}, otherwise it's not a start message. If we receive
        such a message, raise an error.

        This test handles the case where the status field is not present.
        """
        assume(ACTION_STATUS_FIELD not in message_dict)
        message = WrittenMessage.from_dict(message_dict)
        self.assertRaises(
            InvalidStartMessage, WrittenAction.from_messages, message)

    @given(message_dict=START_ACTION_MESSAGE_DICTS,
           status=(just(FAILED_STATUS) | just(SUCCEEDED_STATUS) | text()))
    def test_invalid_start_message_wrong_status(self, message_dict, status):
        """
        A start message must have an C{ACTION_STATUS_FIELD} with the value
        C{STARTED_STATUS}, otherwise it's not a start message. If we receive
        such a message, raise an error.

        This test handles the case where the status field is present, but is
        not C{STARTED_STATUS}.
        """
        message = WrittenMessage.from_dict(
            message_dict.update({ACTION_STATUS_FIELD: status}))
        self.assertRaises(
            InvalidStartMessage, WrittenAction.from_messages, message)

    @given(START_ACTION_MESSAGE_DICTS, integers(min_value=2))
    def test_invalid_task_level_in_start_message(self, start_message_dict, i):
        """
        All messages in an action have a task level. The first message in an
        action must have a task level ending in C{1}, indicating that it's the
        first message.

        If we try to start an action with a message that has a task level that
        does not end in C{1}, raise an error.
        """
        new_level = start_message_dict[TASK_LEVEL_FIELD].append(i)
        message_dict = start_message_dict.set(TASK_LEVEL_FIELD, new_level)
        message = WrittenMessage.from_dict(message_dict)
        self.assertRaises(
            InvalidStartMessage, WrittenAction.from_messages, message)

    @given(START_ACTION_MESSAGES, MESSAGE_DICTS, text(), integers(min_value=1))
    def test_action_type_mismatch(self, start_message, end_message_dict,
                                  end_type, n):
        """
        The end message of an action must have the same C{ACTION_TYPE_FIELD} as
        the start message of an action. If we try to end an action with a
        message that has a different type, we raise an error.
        """
        assume(end_type != start_message.contents[ACTION_TYPE_FIELD])
        end_message = WrittenMessage.from_dict(union(end_message_dict, {
            ACTION_STATUS_FIELD: SUCCEEDED_STATUS,
            ACTION_TYPE_FIELD: end_type,
            TASK_UUID_FIELD: start_message.task_uuid,
            TASK_LEVEL_FIELD: sibling_task_level(start_message, n),
        }))
        self.assertRaises(
            WrongActionType,
            WrittenAction.from_messages, start_message, end_message=end_message)

    @given(START_ACTION_MESSAGES, MESSAGE_DICTS, integers(min_value=2))
    def test_successful_end(self, start_message, end_message_dict, n):
        """
        A L{WrittenAction} can be constructed with just a start message and an end
        message: in this case, an end message that indicates the action was
        successful.

        Such an action inherits the C{end_time} from the end message, and has
        a C{status} of C{SUCCEEDED_STATUS}.
        """
        end_message = WrittenMessage.from_dict(
            union(end_message_dict,
                {ACTION_STATUS_FIELD: SUCCEEDED_STATUS,
                 ACTION_TYPE_FIELD: start_message.contents[ACTION_TYPE_FIELD],
                 TASK_UUID_FIELD: start_message.task_uuid,
                 TASK_LEVEL_FIELD: sibling_task_level(start_message, n),
                }
            ))
        action = WrittenAction.from_messages(
            start_message, end_message=end_message)
        self.assertThat(
            action, MatchesStructure.byEquality(
                action_type=start_message.contents[ACTION_TYPE_FIELD],
                status=SUCCEEDED_STATUS,
                task_uuid=start_message.task_uuid,
                task_level=start_message.task_level.parent(),
                start_time=start_message.timestamp,
                children=pvector([]),
                end_time=end_message.timestamp,
                reason=None,
                exception=None,
            ))

    @given(START_ACTION_MESSAGES, MESSAGE_DICTS, text(), text(),
           integers(min_value=2))
    def test_failed_end(self, start_message, end_message_dict, exception,
                        reason, n):
        """
        A L{WrittenAction} can be constructed with just a start message and an end
        message: in this case, an end message that indicates that the action
        failed.

        Such an action inherits the C{end_time} from the end message, has a
        C{status} of C{FAILED_STATUS}, and an C{exception} and C{reason} that
        match the raised exception.
        """
        end_message = WrittenMessage.from_dict(
            union(end_message_dict,
                {ACTION_STATUS_FIELD: FAILED_STATUS,
                 ACTION_TYPE_FIELD: start_message.contents[ACTION_TYPE_FIELD],
                 TASK_UUID_FIELD: start_message.task_uuid,
                 TASK_LEVEL_FIELD: sibling_task_level(start_message, n),
                 EXCEPTION_FIELD: exception,
                 REASON_FIELD: reason,
                }
            ))
        action = WrittenAction.from_messages(
            start_message, end_message=end_message)
        self.assertThat(
            action, MatchesStructure.byEquality(
                action_type=start_message.contents[ACTION_TYPE_FIELD],
                status=FAILED_STATUS,
                task_uuid=start_message.task_uuid,
                task_level=start_message.task_level.parent(),
                start_time=start_message.timestamp,
                children=pvector([]),
                end_time=end_message.timestamp,
                reason=reason,
                exception=exception,
            ))

    @given(START_ACTION_MESSAGES, MESSAGE_DICTS, integers(min_value=2))
    def test_end_has_no_status(self, start_message, end_message_dict, n):
        """
        If we try to end a L{WrittenAction} with a message that lacks an
        C{ACTION_STATUS_FIELD}, we raise an error, because it's not a valid
        end message.
        """
        assume(ACTION_STATUS_FIELD not in end_message_dict)
        end_message = WrittenMessage.from_dict(
            union(end_message_dict, {
                ACTION_TYPE_FIELD: start_message.contents[ACTION_TYPE_FIELD],
                TASK_UUID_FIELD: start_message.task_uuid,
                TASK_LEVEL_FIELD: sibling_task_level(start_message, n),
            }))
        self.assertRaises(
            InvalidStatus,
            WrittenAction.from_messages, start_message, end_message=end_message)

    # This test is slow, and when run under coverage on pypy on Travis won't
    # make the default of 5 examples. 1 is enough.
    @given(START_ACTION_MESSAGES, lists(WRITTEN_MESSAGES | WRITTEN_ACTIONS),
           settings=Settings(min_satisfying_examples=1))
    def test_children(self, start_message, child_messages):
        """
        We can construct a L{WrittenAction} with child messages. These messages
        can be either L{WrittenAction}s or L{WrittenMessage}s. They are
        available in the C{children} field.
        """
        messages = [
            _reparent_action(
                start_message.task_uuid,
                TaskLevel(level=sibling_task_level(start_message, i)),
                message)
            for (i, message) in enumerate(child_messages, 2)]
        action = WrittenAction.from_messages(start_message, messages)
        task_level = lambda m: m.task_level
        self.assertEqual(
            sorted(set(messages), key=task_level), action.children)

    @given(START_ACTION_MESSAGES, MESSAGE_DICTS)
    def test_wrong_task_uuid(self, start_message, child_message):
        """
        All child messages of an action must have the same C{task_uuid} as the
        action.
        """
        assume(child_message[TASK_UUID_FIELD] != start_message.task_uuid)
        message = WrittenMessage.from_dict(child_message)
        self.assertRaises(
            WrongTask,
            WrittenAction.from_messages, start_message, v(message))

    @given(START_ACTION_MESSAGES, MESSAGE_DICTS)
    def test_wrong_task_level(self, start_message, child_message):
        """
        All child messages of an action must have a task level that is a direct
        child of the action's task level.
        """
        assume(not start_message.task_level.is_sibling_of(
            TaskLevel(level=child_message[TASK_LEVEL_FIELD])))
        message = WrittenMessage.from_dict(
            child_message.update({TASK_UUID_FIELD: start_message.task_uuid}))
        self.assertRaises(
            WrongTaskLevel,
            WrittenAction.from_messages, start_message, v(message))

    @given(START_ACTION_MESSAGES, MESSAGE_DICTS, MESSAGE_DICTS, integers(min_value=2))
    def test_duplicate_task_level(self, start_message, child1, child2, index):
        """
        If we try to add a child to an action that has a task level that's the
        same as the task level of an existing child, we raise an error.
        """
        parent_level = start_message.task_level.parent().level
        messages = [
            WrittenMessage.from_dict(
                union(child_message, {
                    TASK_UUID_FIELD: start_message.task_uuid,
                    TASK_LEVEL_FIELD: parent_level.append(index),
                }))
            for child_message in [child1, child2]]
        assume(messages[0] != messages[1])
        self.assertRaises(
            DuplicateChild,
            WrittenAction.from_messages, start_message, messages)
