"""
Architecture builder classes.
Each builder translates a config dict into an actual PyTorch nn.Module.
Used by training_engine.py for scratch training.
"""
import torch
import torch.nn as nn
import math


class ModelArchitectureBuilder:
    """Base class for architecture builders."""

    def build_config(self, params: dict) -> dict:
        """Build a HuggingFace-compatible config dict."""
        raise NotImplementedError

    def build_model(self, params: dict) -> nn.Module:
        """Build and return a PyTorch model."""
        raise NotImplementedError

    @staticmethod
    def count_parameters(model: nn.Module) -> int:
        return sum(p.numel() for p in model.parameters())


class GPTDecoderBuilder(ModelArchitectureBuilder):
    """GPT-2 style autoregressive decoder."""

    def build_config(self, params: dict) -> dict:
        from transformers import GPT2Config
        dropout = params.get("embd_dropout", 0.1)
        return GPT2Config(
            vocab_size=params.get("vocab_size", 50000),
            n_positions=params.get("max_seq_length", 2048),
            n_embd=params.get("hidden_size", 768),
            n_layer=params.get("num_layers", 12),
            n_head=params.get("num_attention_heads", 12),
            n_inner=params.get("intermediate_size", params.get("hidden_size", 768) * 4),
            activation_function=_map_activation(params.get("activation", "gelu")),
            resid_pdrop=dropout,
            embd_pdrop=dropout,
            attn_pdrop=params.get("attn_dropout", dropout),
        )

    def build_model(self, params: dict) -> nn.Module:
        from transformers import GPT2LMHeadModel
        return GPT2LMHeadModel(self.build_config(params))


class LLaMABuilder(ModelArchitectureBuilder):
    """LLaMA-style decoder with RoPE + RMSNorm + SwiGLU."""

    def build_config(self, params: dict) -> dict:
        from transformers import LlamaConfig
        hs = params.get("hidden_size", 768)
        return LlamaConfig(
            vocab_size=params.get("vocab_size", 50000),
            hidden_size=hs,
            intermediate_size=params.get("intermediate_size", hs * 4),
            num_hidden_layers=params.get("num_layers", 12),
            num_attention_heads=params.get("num_attention_heads", 12),
            num_key_value_heads=params.get("kv_heads", params.get("num_attention_heads", 12)),
            max_position_embeddings=params.get("max_seq_length", 2048),
            rms_norm_eps=params.get("rms_norm_eps", 1e-6),
            rope_theta=params.get("rope_theta", 10000),
            tie_word_embeddings=params.get("tie_word_embeddings", False),
            hidden_act="silu",
            attention_bias=params.get("qkv_bias", False),
        )

    def build_model(self, params: dict) -> nn.Module:
        from transformers import LlamaForCausalLM
        return LlamaForCausalLM(self.build_config(params))


class BERTEncoderBuilder(ModelArchitectureBuilder):
    """BERT-style bidirectional encoder."""

    def build_config(self, params: dict) -> dict:
        from transformers import BertConfig
        hs = params.get("hidden_size", 768)
        dropout = params.get("embd_dropout", 0.1)
        return BertConfig(
            vocab_size=params.get("vocab_size", 50000),
            hidden_size=hs,
            intermediate_size=params.get("intermediate_size", hs * 4),
            num_hidden_layers=params.get("num_layers", 12),
            num_attention_heads=params.get("num_attention_heads", 12),
            max_position_embeddings=params.get("max_seq_length", 2048),
            hidden_dropout_prob=dropout,
            attention_probs_dropout_prob=params.get("attn_dropout", dropout),
        )

    def build_model(self, params: dict) -> nn.Module:
        from transformers import BertForMaskedLM
        return BertForMaskedLM(self.build_config(params))


class MoEBuilder(ModelArchitectureBuilder):
    """Mixture-of-Experts model (LLaMA-style backbone with sparse MoE FFN layers)."""

    def build_config(self, params: dict) -> dict:
        from transformers import LlamaConfig
        hs = params.get("hidden_size", 768)
        return LlamaConfig(
            vocab_size=params.get("vocab_size", 50000),
            hidden_size=hs,
            intermediate_size=params.get("intermediate_size", hs * 4),
            num_hidden_layers=params.get("num_layers", 12),
            num_attention_heads=params.get("num_attention_heads", 12),
            num_key_value_heads=params.get("kv_heads", params.get("num_attention_heads", 12)),
            max_position_embeddings=params.get("max_seq_length", 2048),
            rms_norm_eps=params.get("rms_norm_eps", 1e-6),
            rope_theta=params.get("rope_theta", 10000),
            tie_word_embeddings=params.get("tie_word_embeddings", False),
        )

    def build_model(self, params: dict) -> nn.Module:
        from transformers import LlamaForCausalLM
        model = LlamaForCausalLM(self.build_config(params))
        try:
            self._inject_moe_layers(model, params)
        except Exception as e:
            print(f"[MoE] Could not inject MoE layers: {e}. Using dense model.")
        return model

    def _inject_moe_layers(self, model, params: dict):
        """Replace FFN layers with MoE blocks where possible."""
        hs = params.get("hidden_size", 768)
        inter = params.get("intermediate_size", hs * 4)
        num_experts = params.get("num_experts", 8)
        active_experts = params.get("active_experts", 2)

        # Attempt to use HF's Mixtral MoE if available
        for layer in model.model.layers:
            if hasattr(layer, 'mlp'):
                layer.mlp = SparseMoEBlock(
                    hidden_size=hs,
                    intermediate_size=inter,
                    num_experts=num_experts,
                    top_k=active_experts,
                )


class SparseMoEBlock(nn.Module):
    """Simple MoE block with top-k gating."""

    def __init__(self, hidden_size, intermediate_size, num_experts=8, top_k=2):
        super().__init__()
        self.num_experts = num_experts
        self.top_k = top_k
        self.gate = nn.Linear(hidden_size, num_experts, bias=False)
        self.experts = nn.ModuleList([
            Expert(hidden_size, intermediate_size) for _ in range(num_experts)
        ])

    def forward(self, x):
        B, T, D = x.shape
        x_flat = x.view(-1, D)
        router_logits = self.gate(x_flat)  # (B*T, E)
        router_probs = torch.softmax(router_logits, dim=-1)
        top_k_probs, top_k_indices = torch.topk(router_probs, self.top_k, dim=-1)
        top_k_probs = top_k_probs / top_k_probs.sum(dim=-1, keepdim=True)

        output = torch.zeros_like(x_flat)
        for k in range(self.top_k):
            expert_idx = top_k_indices[:, k]
            expert_weight = top_k_probs[:, k].unsqueeze(-1)
            for e in range(self.num_experts):
                mask = (expert_idx == e)
                if mask.any():
                    expert_out = self.experts[e](x_flat[mask])
                    output[mask] += expert_weight[mask] * expert_out
        return output.view(B, T, D)


class Expert(nn.Module):
    """Single FFN expert (SwiGLU style)."""

    def __init__(self, hidden_size, intermediate_size):
        super().__init__()
        self.gate_proj = nn.Linear(hidden_size, intermediate_size, bias=False)
        self.up_proj = nn.Linear(hidden_size, intermediate_size, bias=False)
        self.down_proj = nn.Linear(intermediate_size, hidden_size, bias=False)

    def forward(self, x):
        return self.down_proj(nn.functional.silu(self.gate_proj(x)) * self.up_proj(x))


class MambaBuilder(ModelArchitectureBuilder):
    """Mamba/SSM model builder."""

    def build_model(self, params: dict) -> nn.Module:
        try:
            from mamba_ssm import Mamba, MambaConfig
        except ImportError:
            raise ImportError(
                "mamba-ssm is required for Mamba models. "
                "Install with: pip install mamba-ssm"
            )
        hs = params.get("hidden_size", 768)
        config = MambaConfig(
            d_model=hs,
            n_layer=params.get("num_layers", 12),
            d_state=params.get("d_state", 16),
            d_conv=params.get("d_conv", 4),
            expand=params.get("expand_factor", 2),
            vocab_size=params.get("vocab_size", 50000),
        )
        return Mamba(config)


class DiTBuilder(ModelArchitectureBuilder):
    """Diffusion Transformer builder."""

    def build_model(self, params: dict) -> nn.Module:
        from .codegen import _generate_dit_model
        # Use codegen approach for DiT
        hs = params.get("hidden_size", 768)
        num_layers = params.get("num_layers", 12)
        num_heads = params.get("num_attention_heads", 12)
        patch_size = params.get("patch_size", 2)
        in_channels = params.get("in_channels", 4)

        class DiTBlock(nn.Module):
            def __init__(self, hidden_size=hs, num_heads=num_heads):
                super().__init__()
                self.norm1 = nn.LayerNorm(hidden_size, elementwise_affine=False)
                self.attn = nn.MultiheadAttention(hidden_size, num_heads, batch_first=True)
                self.norm2 = nn.LayerNorm(hidden_size, elementwise_affine=False)
                self.mlp = nn.Sequential(
                    nn.Linear(hidden_size, hidden_size * 4), nn.GELU(),
                    nn.Linear(hidden_size * 4, hidden_size),
                )
                self.adaLN_modulation = nn.Sequential(
                    nn.SiLU(), nn.Linear(hidden_size, 6 * hidden_size)
                )

            def forward(self, x, c):
                msa_s, msa_sc, msa_g, mlp_s, mlp_sc, mlp_g = \
                    self.adaLN_modulation(c).chunk(6, dim=-1)
                xn = self.norm1(x) * (1 + msa_sc.unsqueeze(1)) + msa_s.unsqueeze(1)
                a, _ = self.attn(xn, xn, xn)
                x = x + msa_g.unsqueeze(1) * a
                xn = self.norm2(x) * (1 + mlp_sc.unsqueeze(1)) + mlp_s.unsqueeze(1)
                x = x + mlp_g.unsqueeze(1) * self.mlp(xn)
                return x

        class DiTModel(nn.Module):
            def __init__(self):
                super().__init__()
                self.patch_embed = nn.Conv2d(in_channels, hs, kernel_size=patch_size, stride=patch_size)
                self.pos_embed = nn.Parameter(torch.zeros(1, 1024, hs))
                self.blocks = nn.ModuleList([DiTBlock() for _ in range(num_layers)])
                self.final_norm = nn.LayerNorm(hs, elementwise_affine=False)
                self.final_conv = nn.ConvTranspose2d(hs, params.get("out_channels", in_channels), kernel_size=patch_size, stride=patch_size)

            def forward(self, x, t_emb):
                x = self.patch_embed(x).flatten(2).transpose(1, 2)
                x = x + self.pos_embed[:, :x.shape[1]]
                for blk in self.blocks:
                    x = blk(x, t_emb)
                x = self.final_norm(x)
                L = x.shape[1]
                Hp = int(math.sqrt(L))
                x = x.transpose(1, 2).reshape(x.shape[0], -1, Hp, Hp)
                return self.final_conv(x)

        return DiTModel()


class CustomBuilder(ModelArchitectureBuilder):
    """Builder for component-assembled architectures."""

    def build_model(self, params: dict) -> nn.Module:
        from .codegen import _generate_custom_model
        # Execute the generated code string to get the model class
        code = _generate_custom_model(params)
        namespace = {}
        exec(code, namespace)
        model, _ = namespace["create_model"]()
        return model


# ── Builder registry ──

BUILDERS = {
    "gpt_decoder": GPTDecoderBuilder(),
    "llama": LLaMABuilder(),
    "bert_encoder": BERTEncoderBuilder(),
    "moe": MoEBuilder(),
    "mamba_ssm": MambaBuilder(),
    "diffusion_dit": DiTBuilder(),
    "custom_builder": CustomBuilder(),
}


def get_builder(arch: str) -> ModelArchitectureBuilder:
    """Get the builder for an architecture type."""
    return BUILDERS.get(arch, GPTDecoderBuilder())


def _map_activation(act: str) -> str:
    """Map our activation names to HF GPT2Config activation names."""
    mapping = {
        "gelu": "gelu",
        "gelu_new": "gelu_new",
        "silu": "silu",
        "swiglu": "gelu",  # GPT2Config doesn't support SwiGLU natively
        "relu": "relu",
    }
    return mapping.get(act, "gelu")
