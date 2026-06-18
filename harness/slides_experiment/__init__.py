"""C4 experiment harness — constrained-vs-free slide schema evaluation.

Answers Dylan's question: "is a constrained deck schema even viable, or does
more-determinism hurt deck quality?"

Three emit strategies × 12 representative prompts × 2 models.

Run:
    cd /tmp/disco-wt-c4exp && source .disco-env
    source ~/.config/disco/agent.env
    python3 -m harness.slides_experiment.run --out /tmp/c4exp-results.json
"""
