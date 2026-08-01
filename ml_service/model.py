"""
PyTorch model for FOLLOWBACK binary classification.

Architecture:
    Input  →  Linear(hidden_dim)
           →  ReLU
           →  Linear(hidden_dim // 2)
           →  ReLU
           →  Linear(1)
           →  Sigmoid
           →  [0 or 1]

The input dimension is determined at construction time
from the feature vector length.
"""

import torch
import torch.nn as nn


class FollowbackPredictor(nn.Module):
    """Simple feed-forward classifier for predicting mutual-follow likelihood."""

    def __init__(self, input_dim, hidden_dim=64):
        super().__init__()
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim

        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.ReLU(),
            nn.Linear(hidden_dim // 2, 1),
            nn.Sigmoid(),
        )

    def forward(self, x):
        """Returns probability (0-1) of mutual follow."""
        return self.net(x)

    def predict(self, x):
        """Returns binary prediction: 0 or 1."""
        with torch.no_grad():
            prob = self.forward(x)
            return (prob >= 0.5).int().squeeze(-1)

    def predict_proba(self, x):
        """Returns probability (0-1) without gradient tracking."""
        with torch.no_grad():
            return self.forward(x).squeeze(-1)
