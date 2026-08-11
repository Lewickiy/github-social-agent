"""
PyTorch model for FOLLOWBACK binary classification.

Architecture:
    Input  →  Linear(hidden_dim)
           →  ReLU
           →  [Dropout]
           →  Linear(hidden_dim // 2)
           →  ReLU
           →  [Dropout]
           →  Linear(1)
           →  Sigmoid
           →  [0 or 1]

The input dimension is determined at construction time
from the feature vector length.  ``dropout`` defaults to 0.0
(no dropout layers), which keeps the architecture identical
to older saved checkpoints — so old models load unchanged.
"""

import torch
import torch.nn as nn


class FollowbackPredictor(nn.Module):
    """Simple feed-forward classifier for predicting mutual-follow likelihood."""

    def __init__(self, input_dim, hidden_dim=64, dropout=0.0):
        super().__init__()
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.dropout = dropout

        layers = [
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
        ]
        if dropout > 0:
            layers.append(nn.Dropout(dropout))
        layers += [
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.ReLU(),
        ]
        if dropout > 0:
            layers.append(nn.Dropout(dropout))
        layers += [
            nn.Linear(hidden_dim // 2, 1),
            nn.Sigmoid(),
        ]
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        """Returns probability (0-1) of mutual follow."""
        return self.net(x)

    def predict(self, x, threshold=0.5):
        """Returns binary prediction: 0 or 1.

        *threshold* is the decision line on the followback probability
        (default 0.5 — the classic decision rule).  Callers pass the
        runtime ML follow threshold (Management-tab dial) so the stored
        0/1 prediction always reflects the current strictness.
        """
        with torch.no_grad():
            prob = self.forward(x)
            return (prob >= threshold).int().squeeze(-1)

    def predict_proba(self, x):
        """Returns probability (0-1) without gradient tracking."""
        with torch.no_grad():
            return self.forward(x).squeeze(-1)
