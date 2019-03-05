# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.


# -*- coding: utf-8 -*-
import os
import re
import sys
import textwrap
import subprocess
from pkg_resources import Requirement, resource_filename

from typing import Mapping, Union, Tuple, List

import numpy as np

from glk import ffi, lib
from io import StringIO

import textworld
from textworld.utils import str2bool
from textworld.generator.game import Game, GameProgression
from textworld.generator.inform7 import Inform7Game
from textworld.logic import Action, State
from textworld.core import GameNotRunningError

GLULX_PATH = resource_filename(Requirement.parse('textworld'), 'textworld/thirdparty/glulx/Git-Glulx')


class MissingGameInfosError(NameError):
    """
    Thrown if an action requiring GameInfos is used on a game without GameInfos, such as a Frotz game or a
    Glulx game not generated by TextWorld.
    """

    def __init__(self):
        msg = ("Can only use GitGlulxMLEnvironment with games generated by "
               " TextWorld. Make sure the generated .json file is in the same "
               " folder as the .ulx game file.")
        super().__init__(msg)


class StateTrackingIsRequiredError(NameError):
    """
    Thrown if an action requiring state tracking is performed while state tracking is not enabled.
    """

    def __init__(self, info):
        msg = ("To access '{}', state tracking need to be activated first."
               " Make sure env.activate_state_tracking() is called before"
               " env.reset().")
        super().__init__(msg.format(info))


class OraclePolicyIsRequiredError(NameError):
    """
    Thrown if an action requiring an Oracle-based reward policy is called without the intermediate reward being active.
    """

    def __init__(self, info):
        msg = ("To access '{}', intermediate reward need to be activated first."
               " Make sure env.compute_intermediate_reward() is called *before* env.reset().")
        super().__init__(msg.format(info))


class ExtraInfosIsMissingError(NameError):
    """
    Thrown if extra information is required without enabling it first via `tw-extra-infos CMD`.
    """

    def __init__(self, info):
        msg = ("To access extra info '{info}', it needs to be enabled via `tw-extra-infos {info}` first."
               " Make sure env.enable_extra_info({info}) is called *before* env.reset().")
        super().__init__(msg.format(info=info))


def _strip_input_prompt_symbol(text: str) -> str:
    if text.endswith("\n>"):
        return text[:-2]

    return text


def _strip_i7_event_debug_tags(text: str) -> str:
    _, text = _detect_i7_events_debug_tags(text)
    return text


def _detect_extra_infos(text: str) -> Mapping[str, str]:
    """ Detect extra information printed out at every turn.

    Extra information can be enabled via the special command:
    `tw-extra-infos COMMAND`. The extra information is displayed
    between tags that look like this: <COMMAND> ... </COMMAND>.

    Args:
        text: Text outputted by the game.

    Returns:
        A dictionary where the keys are text commands and the corresponding
        values are the extra information displayed between tags.
    """
    tags = ["description", "inventory", "score"]
    matches = {}
    for tag in tags:
        regex = re.compile(r"<{tag}>\n(.*)</{tag}>".format(tag=tag), re.DOTALL)
        match = re.search(regex, text)
        if match:
            _, cleaned_text = _detect_i7_events_debug_tags(match.group(1))
            matches[tag] = cleaned_text
            text = re.sub(regex, "", text)

    return matches, text


def _detect_i7_events_debug_tags(text: str) -> Tuple[List[str], str]:
    """ Detect all Inform7 events debug tags.

    In Inform7, debug tags look like this: [looking], [looking - succeeded].

    Args:
        text: Text outputted by the game.

    Returns:
        A tuple containing a list of Inform 7 events that were detected
        in the text, and a cleaned text without Inform 7 debug infos.
    """
    matches = []
    open_tags = []
    for match in re.findall(r"(?<!\x1b)\[[^]]+\]\n?", text):
        text = text.replace(match, "")  # Remove i7 debug tags.
        tag_name = match.strip()[1:-1]  # Strip starting '[' and trailing ']'.

        if " - failed" in tag_name:
            tag_name = tag_name[:tag_name.index(" - failed")]
            open_tags.remove(tag_name)

        elif " - succeeded" in tag_name:
            tag_name = tag_name[:tag_name.index(" - succeeded")]
            open_tags.remove(tag_name)
            matches.append(tag_name)
        else:
            open_tags.append(tag_name)

    # If it's got either a '(' or ')' in it, it's a subrule,
    # so it doesn't count.
    matches = [m for m in matches if "(" not in m and ")" not in m]

    if len(matches) > 0:
        assert len(open_tags) == 0

    return matches, text


class GlulxGameState(textworld.GameState):
    """
    Encapsulates the state of a Glulx game. This is the primary interface to the Glulx
    game driver.
    """

    def __init__(self, *args, **kwargs):
        """
        Takes the same parameters as textworld.GameState
        :param args: The arguments
        :param kwargs: The kwargs
        """
        super().__init__(*args, **kwargs)
        self.has_timeout = False
        self._state_tracking = False
        self._compute_intermediate_reward = False
        self._max_score = 0

    def init(self, output: str, game: Game,
             state_tracking: bool = False, compute_intermediate_reward: bool = False):
        """
        Initialize the game state and set tracking parameters.
        The tracking parameters, state_tracking and compute_intermediate_reward,
        are computationally expensive, so are disabled by default.

        :param output: Introduction text displayed when a game starts.
        :param game: The glulx game to run
        :param state_tracking: Whether to use state tracking
        :param compute_intermediate_reward: Whether to compute the intermediate reward
        """
        output = _strip_input_prompt_symbol(output)
        _, output = _detect_i7_events_debug_tags(output)
        self._extra_infos, output = _detect_extra_infos(output)

        super().init(output)
        self._game = game
        self._game_progression = GameProgression(game, track_quests=state_tracking)
        self._inform7 = Inform7Game(game)
        self._state_tracking = state_tracking
        self._compute_intermediate_reward = compute_intermediate_reward and len(game.quests) > 0
        self._objective = game.objective
        self._score = 0
        self._max_score = self._game_progression.max_score

    def view(self) -> "GlulxGameState":
        """
        Returns a view of this Game as a GameState
        :return: A GameState reflecting the current state
        """
        game_state = GlulxGameState()
        game_state.previous_state = self.previous_state
        game_state._state = self.state
        game_state._state_tracking = self._state_tracking
        game_state._compute_intermediate_reward = self._compute_intermediate_reward
        game_state._command = self.command
        game_state._feedback = self.feedback
        game_state._action = self.action

        game_state._description = self._description if hasattr(self, "_description") else ""
        game_state._inventory = self._inventory if hasattr(self, "_inventory") else ""

        game_state._objective = self.objective
        game_state._score = self.score
        game_state._max_score = self.max_score
        game_state._nb_moves = self.nb_moves
        game_state._has_won = self.has_won
        game_state._has_lost = self.has_lost
        game_state.has_timeout = self.has_timeout

        if self._state_tracking:
            game_state._admissible_commands = self.admissible_commands

        if self._compute_intermediate_reward:
            game_state._policy_commands = self.policy_commands

        return game_state

    def update(self, command: str, output: str) -> "GlulxGameState":
        """
        Updates the GameState with the command from the agent and the output
        from the interpreter.
        :param command: The command sent to the interpreter
        :param output: The output from the interpreter
        :return: A GameState of the current state
        """
        output = _strip_input_prompt_symbol(output)

        # Detect any extra information displayed at every turn.
        extra_infos, output = _detect_extra_infos(output)

        game_state = super().update(command, output)
        game_state.previous_state = self.view()
        game_state._objective = self.objective
        game_state._max_score = self.max_score
        game_state._inform7 = self._inform7
        game_state._game = self._game
        game_state._game_progression = self._game_progression
        game_state._state_tracking = self._state_tracking
        game_state._compute_intermediate_reward = self._compute_intermediate_reward
        game_state._extra_infos = {**self._extra_infos, **extra_infos}

        # Detect what events just happened in the game.
        i7_events, game_state._feedback = _detect_i7_events_debug_tags(output)
        if self._state_tracking:
            for i7_event in i7_events:
                valid_actions = self._game_progression.valid_actions
                game_state._action = self._inform7.detect_action(i7_event, valid_actions)
                if game_state._action is not None:
                    # An action that affects the state of the game.
                    game_state._game_progression.update(game_state._action)

        return game_state

    @property
    def description(self):
        if not hasattr(self, "_description"):
            if "description" not in self._extra_infos:
                raise ExtraInfosIsMissingError("description")

            self._description = self._extra_infos["description"]

        return self._description

    @property
    def inventory(self):
        if not hasattr(self, "_inventory"):
            if "inventory" not in self._extra_infos:
                raise ExtraInfosIsMissingError("inventory")

            self._inventory = self._extra_infos["inventory"]

        return self._inventory

    @property
    def command_feedback(self):
        """ Return the parser response related to the previous command.

        This corresponds to the feedback without the room description,
        the inventory and the objective (if they are present).
        """
        if not hasattr(self, "_command_feedback"):
            command_feedback = self.feedback

            # On the first move, command_feedback should be empty.
            if self.nb_moves == 0:
                command_feedback = ""

            # Remove room description from command feedback.
            if len(self.description.strip()) > 0:
                regex = "\s*" + re.escape(self.description.strip()) + "\s*"
                command_feedback = re.sub(regex, "", command_feedback)

            # Remove room inventory from command feedback.
            if len(self.inventory.strip()) > 0:
                regex = "\s*" + re.escape(self.inventory.strip()) + "\s*"
                command_feedback = re.sub(regex, "", command_feedback)

            # Remove room objective from command feedback.
            if len(self.objective.strip()) > 0:
                regex = "\s*" + re.escape(self.objective.strip()) + "\s*"
                command_feedback = re.sub(regex, "", command_feedback)

            self._command_feedback = command_feedback.strip()

        return self._command_feedback

    @property
    def objective(self):
        """ Objective of the game. """
        return self._objective

    @property
    def policy_commands(self):
        """ Commands to entered in order to complete the quest. """
        if not hasattr(self, "_policy_commands"):
            if not self._compute_intermediate_reward:
                raise OraclePolicyIsRequiredError("policy_commands")

            self._policy_commands = []
            if self._game_progression.winning_policy is not None:
                winning_policy = self._game_progression.winning_policy
                self._policy_commands = self._inform7.gen_commands_from_actions(winning_policy)

        return self._policy_commands

    @property
    def intermediate_reward(self):
        """ Reward indicating how useful the last action was for solving the quest. """
        if not self._compute_intermediate_reward:
            raise OraclePolicyIsRequiredError("intermediate_reward")

        if self.has_won:
            # The last action led to winning the game.
            return 1

        if self.has_lost:
            # The last action led to losing the game.
            return -1

        if self.previous_state is None:
            return 0

        return np.sign(len(self.previous_state.policy_commands) - len(self.policy_commands))

    @property
    def score(self):
        if not hasattr(self, "_score"):
            if self._state_tracking:
                self._score = self._game_progression.score
            else:
                if "score" not in self._extra_infos:
                    raise ExtraInfosIsMissingError("score")

                self._score = int(self._extra_infos["score"])

        return self._score

    @property
    def max_score(self):
        return self._max_score

    @property
    def has_won(self):
        if not hasattr(self, "_has_won"):
            if self._compute_intermediate_reward:
                self._has_won = self._game_progression.completed
            else:
                self._has_won = '*** The End ***' in self.feedback

        return self._has_won

    @property
    def has_lost(self):
        if not hasattr(self, "_has_lost"):
            if self._compute_intermediate_reward:
                self._has_lost = self._game_progression.failed
            else:
                self._has_lost = '*** You lost! ***' in self.feedback

        return self._has_lost

    @property
    def game_ended(self) -> bool:
        """ Whether the game is finished or not. """
        return self.has_won | self.has_lost | self.has_timeout

    @property
    def game_infos(self) -> Mapping:
        """ Additional information about the game. """
        return self._game.infos

    @property
    def state(self) -> State:
        """ Current game state. """
        if not hasattr(self, "_state"):
            self._state = self._game_progression.state.copy()

        return self._state

    @property
    def action(self) -> Action:
        """ Last action that was detected. """
        if not hasattr(self, "_action"):
            return None

        return self._action

    @property
    def admissible_commands(self):
        """ Return the list of admissible commands given the current state. """
        if not hasattr(self, "_admissible_commands"):
            if not self._state_tracking:
                raise StateTrackingIsRequiredError("admissible_commands")

            all_valid_commands = self._inform7.gen_commands_from_actions(self._game_progression.valid_actions)
            # To guarantee the order from one execution to another, we sort the commands.
            # Remove any potential duplicate commands (they would lead to the same result anyway).
            self._admissible_commands = sorted(set(all_valid_commands))

        return self._admissible_commands

    @property
    def command_templates(self):
        return self._game.command_templates

    @property
    def verbs(self):
        return self._game.verbs

    @property
    def entities(self):
        return self._game.entity_names

    @property
    def extras(self):
        return self._game.extras



class GitGlulxMLEnvironment(textworld.Environment):
    """ Environment to support playing Glulx games generated by TextWorld.

    TextWorld supports playing text-based games that were compiled for the
    `Glulx virtual machine <https://www.eblong.com/zarf/glulx>`_. The main
    advantage of using Glulx over Z-Machine is it uses 32-bit data and
    addresses, so it can handle game files up to four gigabytes long. This
    comes handy when we want to generate large world with a lot of objects
    in it.

    We use a customized version of `git-glulx <https://github.com/DavidKinder/Git>`_
    as the glulx interpreter. That way we don't rely on stdin/stdout to
    communicate with the interpreter but instead use UNIX message queues.

    """
    metadata = {'render.modes': ['human', 'ansi', 'text']}

    def __init__(self, gamefile: str) -> None:
        """ Creates a GitGlulxML from the given gamefile

        Args:
            gamefile: The name of the gamefile to load.
        """
        super().__init__()
        self._gamefile = gamefile
        self._process = None

        # Load initial state of the game.
        filename, ext = os.path.splitext(gamefile)
        game_json = filename + ".json"

        if not os.path.isfile(game_json):
            raise MissingGameInfosError()

        self._state_tracking = False
        self._compute_intermediate_reward = False
        self.game = Game.load(game_json)
        self.game_state = None
        self.extra_info = set()

    def enable_extra_info(self, info) -> None:
        self.extra_info.add(info)

    def activate_state_tracking(self) -> None:
        self._state_tracking = True

    def compute_intermediate_reward(self) -> None:
        self._compute_intermediate_reward = True

    @property
    def game_running(self) -> bool:
        """ Determines if the game is still running. """
        return self._process is not None and self._process.poll() is None

    def step(self, command: str) -> Tuple[GlulxGameState, float, bool]:
        if not self.game_running:
            raise GameNotRunningError()

        command = command.strip()
        output = self._send(command)
        if output is None:
            raise GameNotRunningError()

        self.game_state = self.game_state.update(command, output)
        self.game_state.has_timeout = not self.game_running
        return self.game_state, self.game_state.score, self.game_state.game_ended

    def _send(self, command: str) -> Union[str, None]:
        if not self.game_running:
            return None

        if len(command) == 0:
            command = " "

        c_command = ffi.new('char[]', command.encode('utf-8'))
        result = lib.communicate(self._names_struct, c_command)
        if result == ffi.NULL:
            self.close()
            return None

        result = ffi.gc(result, lib.free)
        result = ffi.string(result).decode('utf-8')
        result = result.replace("\\033[", "\033[")

        return result

    def reset(self) -> GlulxGameState:
        if self.game_running:
            self.close()

        self._names_struct = ffi.new('struct sock_names*')

        lib.init_glulx(self._names_struct)
        sock_name = ffi.string(self._names_struct.sock_name).decode('utf-8')
        self._process = subprocess.Popen(["%s/git-glulx-ml" % (GLULX_PATH,), self._gamefile, '-g', sock_name, '-q'])
        c_feedback = lib.get_output_nosend(self._names_struct)
        if c_feedback == ffi.NULL:
            self.close()
            raise ValueError("Game failed to start properly: {}.".format(self._gamefile))
        c_feedback = ffi.gc(c_feedback, lib.free)

        start_output = ffi.string(c_feedback).decode('utf-8')
        start_output = start_output.replace("\\033[", "\033[")


        if not self._state_tracking:
            self.enable_extra_info("score")

        # TODO: check if the game was compiled in debug mode. You could parse
        #       the output of the following command to check whether debug mode
        #       was used or not (i.e. invalid action not found).
        self._send('tw-trace-actions')  # Turn on debug print for Inform7 action events.
        _extra_output = ""
        for info in self.extra_info:
            _extra_output = self._send('tw-extra-infos {}'.format(info))

        start_output = start_output[:-1] + _extra_output[:-1]  # Add extra infos minus the prompts '>'.
        self.game_state = GlulxGameState(self)
        self.game_state.init(start_output, self.game, self._state_tracking, self._compute_intermediate_reward)

        return self.game_state

    def close(self) -> None:
        if self.game_running:
            self._process.kill()
            self._process.wait()
            self._process = None

        try:
            lib.cleanup_glulx(self._names_struct)
        except AttributeError:
            pass  # Attempted to kill before reset

    def render(self, mode: str = "human") -> None:
        outfile = StringIO() if mode in ['ansi', "text"] else sys.stdout

        msg = self.game_state.feedback.rstrip() + "\n"
        if str2bool(os.environ.get("TEXTWORLD_DEBUG", False)):
            msg = self.game_state._raw.rstrip() + "\n"

        if self.display_command_during_render and self.game_state.command is not None:
            msg = '> ' + self.game_state.command + "\n" + msg

        # Wrap each paragraph.
        if mode == "human":
            paragraphs = msg.split("\n")
            paragraphs = ["\n".join(textwrap.wrap(paragraph, width=80)) for paragraph in paragraphs]
            msg = "\n".join(paragraphs)

            highlights = re.findall(r"\x1b[^\x1b]+\x1b\[0m", msg)
            for highlight in set(highlights):
                msg = rreplace(msg, highlight, highlight[7:-4], msg.count(highlight) - 1)

        outfile.write(msg + "\n")

        if mode == "text":
            outfile.seek(0)
            return outfile.read()

        if mode == 'ansi':
            return outfile


def rreplace(s, old, new, occurrence):
    li = s.rsplit(old, occurrence)
    return new.join(li)
