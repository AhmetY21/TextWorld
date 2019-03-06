#!/usr/bin/env python

# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.


import os
import re
import json
import base64
import logging
import textwrap
import argparse
import itertools
from typing import List, Mapping, Any, Optional
from collections import defaultdict
from prompt_toolkit import prompt
from html.parser import HTMLParser

import numpy as np
import matplotlib.pyplot as plt

import gym
from gym.utils import colorize

import textworld
import textworld.gym
import textworld.agents
from textworld import EnvInfos


from PIL import Image
from visdom import Visdom
logging.getLogger().setLevel(logging.CRITICAL)


MAX_RANK = 10  # To display
LEADERBOARD_FILE = "./leaderboard.txt"
AI_NAMES_FILE = "ai_names.txt"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("game")
    parser.add_argument("--agent-scores", default="agent_scores.json",
                        help="Path to the agent's scores (as generated by `play_agent.py`).")
    parser.add_argument("--mode", default="human", metavar="MODE",
                        choices=["random", "human", "random-cmd", "walkthrough"],
                        help="Select an agent to play the game: %(choices)s."
                             " Default: %(default)s.")
    parser.add_argument("--max-steps", type=int, default=100, metavar="STEPS",
                        help="Limit maximum number of steps.")
    parser.add_argument("--viewer", metavar="PORT", type=int, nargs="?", const=6070,
                        help="Start web viewer.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose mode.")
    parser.add_argument("-vv", "--very-verbose", action="store_true",
                        help="Print debug information.")

    parser.add_argument("--refresh", action="store_true",
                        help="Only refresh leaderboard.")
    return parser.parse_args()


class MLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.fed = []

    def handle_data(self, d):
        self.fed.append(d)

    def get_data(self):
        return ''.join(self.fed)

    @classmethod
    def strip(cls, html):
        s = cls()
        s.feed(html)
        return s.get_data()


def plot(scores, best, max_score, max_steps):

    if len(scores) == 1:
        scores = scores * 2

    X = np.array([np.arange(max_steps + 1)] * 3).T
    data = np.ones((3, max_steps + 1)) * np.nan
    data[0, :len(scores)] = scores
    data[1, :len(best)] = best
    data[2, :] = max_score

    vis = Visdom(port=4321)
    vis.line(X=X, Y=data.T, win="Summary",
             opts={"legend": ["Player", "Agent", "Max score"],
                   "linecolor": np.array([(0, 255, 255), (255, 0, 0), (0, 0, 0)]),
                   "dash": np.array(["solid", "solid", "dot"]),
                   "layoutopts": {'plotly': {
                                    'line': {'shape': 'hv'},
                                    'font': {'family': 'Courier New, monospace', 'size': 18, 'color': '#7f7f7f'},
                                    'legend': {'xanchor': "right", "borderwidth": 1},
                                    'xaxis': {'title': "Moves", "dtick": 10, },
                                    'yaxis': {"title": "Points", "tickmode": "linear"}
                                    }
                                 }
                  })


def update_leaderboard(name, scores):
    # Load leaderboard
    leaderboard = []
    if os.path.isfile(LEADERBOARD_FILE):
        with open(LEADERBOARD_FILE) as f:
            leaderboard = json.load(f)

    # Update leaderboard
    leaderboard.append((name, scores))
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard, f, indent=2)


def plot_final(max_score, max_steps, game):
    # Load leaderboard
    leaderboard = []
    if os.path.isfile(LEADERBOARD_FILE):
        with open(LEADERBOARD_FILE) as f:
            leaderboard = json.load(f)

    html = """
        <table style="font-family: arial sans-serif; font-size: large; text-align:center; border-collapse: collapse; width: 100%; color: #000;">
            <tr>
                <th style="font-weight: bold; text-align:center;">Rank</th>
                <th style="font-weight: bold; text-align:center;">Name</th>
                <th style="font-weight: bold; text-align:center;">Score</th>
                <th style="font-weight: bold; text-align:center;">Moves</th>
            </tr>
            {rows}
            </table>
        """

    row = """
        <tr>
            <td>{rank}</td>
            <td>{name}</td>
            <td>{score}</td>
            <td>{moves}</td>
        </tr>
    """


    rows = []
    # Sort by highest score than fewest moves.
    for rank, (name, scores) in enumerate(sorted(leaderboard, key=lambda e: (e[-1][-1], -len(e[-1])), reverse=True)):
        if rank + 1 > MAX_RANK:
            break

        rows.append(row.format(rank=rank + 1, name=name, score=scores[-1], moves=len(scores) - 1))

    vis = Visdom(port=4321)
    vis.text(html.format(rows="\n".join(rows)), win="Leaderboard")

    # Plot leaderboard lines.
    nb_entries = min(len(leaderboard), MAX_RANK)
    nb_lines = nb_entries + 1
    X = np.array([np.arange(max_steps + 1)] * nb_lines).T
    data = np.ones((nb_lines, max_steps + 1)) * np.nan
    legend = []
    for rank, (name, scores) in enumerate(sorted(leaderboard, key=lambda e: (e[-1][-1], -len(e[-1])), reverse=True)):
        if rank + 1 > MAX_RANK:
            break

        data[rank, :len(scores)] = scores
        legend.append(name)

    data[-1, :] = max_score
    legend.append("Max score")

    vis.line(X=X, Y=data.T, win="Summary",
             opts={"legend": legend,
                   "dash": np.array(["solid"] * nb_entries + ["dot"]),
                   "layoutopts": {'plotly': {
                                       'font': {'family': 'Courier New, monospace', 'size': 18, 'color': '#7f7f7f'},
                                       #'legend': {'xanchor': "right", "borderwidth": 1},
                                       'legend': {'y': 0.5, "borderwidth": 0},
                                       'xaxis': {'title': "Moves", "dtick": 10, },
                                       'yaxis': {"title": "Points", "tickmode": "linear"}
                                       }
                                   }
                   })

    # Diplay walkthrough map.
    image_path = game.replace(".ulx", ".png")
    # img = Image.open(game.replace(".ulx", ".png"))
    # # img.thumbnail((1024, 1024))
    # # img.thumbnail((764, 764))
    # # img.show()
    # #vis.image(np.array(img).T.swapaxes(1, 2), win="Layout")
    # img.show()
    os.system("eog --disable-gallery " + image_path)
    # plt.figure(figsize=(12, 9))
    # plt.imshow(img)
    # plt.axis('off')
    # plt.tight_layout()
    # plt.show(img)


def main():
    args = parse_args()
    if args.very_verbose:
        args.verbose = args.very_verbose

    with open(args.agent_scores) as f:
        agent_scores = np.array(json.load(f))

    best = np.nanmax(agent_scores, axis=0).astype(int)

    env = textworld.start(args.game)

    if args.mode == "random":
        agent = textworld.agents.NaiveAgent(seed=None)
    elif args.mode == "random-cmd":
        agent = textworld.agents.RandomCommandAgent(seed=None)
    elif args.mode == "human":
        agent = textworld.agents.HumanAgent(autocompletion=True)
    elif args.mode == 'walkthrough':
        agent = textworld.agents.WalkthroughAgent()

    agent.reset(env)
    if args.viewer is not None:
        from textworld.envs.wrappers import HtmlViewer
        env = HtmlViewer(env, port=args.viewer)

    game_state = env.reset()
    if args.refresh:
        plot_final(game_state.max_score, args.max_steps, args.game)
        return

    history = []
    scores = [0]
    if args.mode == "human" or args.verbose:
        text = env.render(mode="text")
        banner, intro = text.split(game_state.objective)
        print(banner[2:], end="")
        paragraphs = intro.split("\n")
        paragraphs = ["\n".join(textwrap.wrap(paragraph, width=80)) for paragraph in paragraphs]
        intro = "\n".join(paragraphs)


    plot(scores, best[:len(scores)], game_state.max_score, args.max_steps)

    try:
        msg = textwrap.dedent("""
        #====================================================================#
        # In this game, you are going to be compared to a trained RL agent.  #
        # The agent has played similar games but it has not seen this one.   #
        # Your score will be shown in blue while the agent's will be in red. #
        #====================================================================#
        """)

        text = ""
        text += colorize(msg.split(" blue ")[0], color="yellow")
        text += colorize(" blue ", color="cyan", bold=True)
        text += colorize(msg.split(" blue ")[-1].split(" red")[0], color="yellow")
        text += colorize(" red", color="red", bold=True)
        text += colorize(msg.split(" red")[-1], color="yellow")

        print(text)
        input("Press [enter] to start...")
        print("\n\n" + colorize(game_state.objective, "yellow", bold=True))
        print(intro)

        # msg_player = "So far, you have scored {}/{} points in {}/{} moves".format(game_state.score, game_state.max_score,
        #                                                                           game_state.nb_moves, args.max_steps)
        #print(colorize(msg_player, "cyan", bold=True))

        # msg_agent  = "while the RL agent has scored {}/{} points in {}/{} moves.".format(best[game_state.nb_moves], game_state.max_score,
        #                                                                                  game_state.nb_moves, args.max_steps)
        #print(colorize(msg_agent, "red", bold=True))
        print("Your score: " + colorize(str(game_state.score), "cyan", bold=True))
        print("Agent's score: " + colorize(str(best[game_state.nb_moves]), "red", bold=True))
        print("\nWhat do you want to do?")

        score = 0
        done = False
        for _ in range(args.max_steps) if args.max_steps > 0 else itertools.count():
            command = agent.act(game_state, score, done)
            game_state, score, done = env.step(command)
            scores.append(score)
            history.append(command)

            if args.mode == "human" or args.verbose:
                env.render()

            # msg_player = "So far, you have scored {}/{} points in {}/{} moves".format(game_state.score, game_state.max_score,
            #                                                                           game_state.nb_moves, args.max_steps)
            #print(colorize(msg_player, "cyan", bold=True))

            agent_score, agent_moves = best[-1], len(best)
            if game_state.nb_moves < len(best):
                agent_score, agent_moves = best[game_state.nb_moves], game_state.nb_moves

            # msg_agent = "while the RL agent has scored {}/{} points in {}/{} moves.".format(agent_score, game_state.max_score,
            #                                                                                 agent_moves, args.max_steps)
            # if game_state.nb_moves >= len(best):
            #     msg_agent += " [Won]" if best[-1] >= game_state.max_score else " [Lost]"

            #print(colorize(msg_agent, "red", bold=True))

            print("Your score: " + colorize(str(game_state.score), "cyan", bold=True))
            print("Agent's score: " + colorize(str(agent_score), "red", bold=True))
            print()

            plot(scores, best[:len(scores)], game_state.max_score, args.max_steps)

            if done:
                break

    except KeyboardInterrupt:
        pass

    env.close()
    print("Done after {}/{} moves with a score of {}/{}.\n".format(game_state.nb_moves, args.max_steps,
                                                                   game_state.score, game_state.max_score))

    with open(AI_NAMES_FILE) as f:
        names = f.read().strip().split("\n")

    default_name = np.random.choice(names)
    name = prompt("Your name for the leaderboard: ", default=default_name)
    name = MLStripper.strip(name)
    while len(name) > 12:
        print(colorize("Name's length is limited to 12 characters!", color="red", bold=True))
        name = prompt("Your name for the leaderboard: ", default=name[:12])
        name = MLStripper.strip(name)

    update_leaderboard(name, scores)
    plot_final(game_state.max_score, args.max_steps, args.game)


if __name__ == "__main__":
    main()