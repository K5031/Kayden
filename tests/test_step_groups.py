"""Consecutive steps fold into one group: the shared labels and the desktop app's grouping."""

from __future__ import annotations

import json

from kayden.api import ToolStatus
from kayden.frontends.common import step_problems, steps_title
from kayden.frontends.desktop.viewmodel import (
    AssistantItem,
    NoticeItem,
    Phase,
    ToolItem,
    UserItem,
    group_heading,
    step_segments,
)


def step(sentence: str | None = None, status: ToolStatus | None = ToolStatus.OK,
         phase: Phase = Phase.DONE) -> ToolItem:
    arguments = {"q": "x"} | ({"description": sentence} if sentence else {})
    return ToolItem("c", "look", json.dumps(arguments), phase=phase, status=status)


def test_titles_and_problems():
    assert steps_title(1) == "1 step" and steps_title(4) == "4 steps"
    assert step_problems(["", "failed", "declined", "failed"]) == "2 failed, 1 declined"
    assert step_problems(["", ""]) == ""


def test_steps_and_the_thinking_before_them_form_one_group():
    user, think, a, b, between = (UserItem("hi"), AssistantItem(reasoning="hmm"), step(),
                                  step(), AssistantItem(reasoning="next"))
    answer = AssistantItem(text="done", reasoning="so")
    assert step_segments([user, think, a, between, b, answer]) == [
        user, [think, a, between, b], answer]


def test_thinking_after_the_last_step_stays_out_until_a_step_follows():
    a, thinking = step(), AssistantItem(reasoning="hmm")
    assert step_segments([a, thinking]) == [[a], thinking]
    b = step()
    assert step_segments([a, thinking, b]) == [[a, thinking, b]]


def test_text_notices_and_lone_thinking_split_groups():
    a, b, c = step(), step(), step()
    reply, notice = AssistantItem(text="x"), NoticeItem("note")
    assert step_segments([a, reply, b, notice, c]) == [[a], reply, [b], notice, [c]]
    thinking = AssistantItem(reasoning="r")
    assert step_segments([thinking]) == [thinking]
    refused = AssistantItem(refused=True)
    assert step_segments([a, refused, b]) == [[a], refused, [b]]


def test_heading_shows_the_running_step_then_a_count():
    done, running = step("Search the web"), step("Open the page", None, Phase.RUNNING)
    assert group_heading([done, running]) == ("Open the page", "running", running)
    running.phase, running.status = Phase.DONE, ToolStatus.ERROR
    assert group_heading([done, running]) == ("2 steps", "1 failed", None)
    running.status = ToolStatus.OK
    assert group_heading([done, running]) == ("2 steps", "", None)


def test_heading_of_one_step_is_its_sentence():
    assert group_heading([step("Search the web")]) == ("Searched the web", "", None)
    assert group_heading([step("Open it", ToolStatus.ERROR)]) == ("Open it", "failed", None)
    stopped = step("Write it", None, Phase.NOT_RUN)
    assert group_heading([stopped]) == ("Write it", "not run", None)
