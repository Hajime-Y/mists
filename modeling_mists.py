from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

import torch
import torch.utils.checkpoint
from torch import nn

from transformers import PreTrainedModel
from transformers.activations import ACT2FN
from transformers import Cache
from transformers.modeling_outputs import ModelOutput
from transformers.utils import (
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    logging,
    replace_return_docstrings,
)
from transformers import AutoModel, AutoModelForCausalLM

from .modeling_moment import MomentEmbeddingModel
from .configuration_mists import MistsConfig


@dataclass
# Copied from transformers.models.idefics.modeling_idefics.IdeficsCausalLMOutputWithPast with Idefics->Mists
class MistsCausalLMOutputWithPast(ModelOutput):
    loss: Optional[torch.FloatTensor] = None
    logits: torch.FloatTensor = None
    past_key_values: Optional[List[torch.FloatTensor]] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None
    time_series_hidden_states: Optional[Tuple[torch.FloatTensor]] = None


class MistsMultiModalProjector(nn.Module):
    def __init__(self, config: MistsConfig):
        super().__init__()

        # time series towerからのoutputは定型でない。input_maskに合わせてpadding用の学習可能なベクトルを使用し、time series towerからの入力を定型にする。
        self.mask_embedding = nn.Parameter(torch.randn(1, 1, config.time_series_hidden_size))
        
        # mlp
        self.linear_1 = nn.Linear(config.time_series_hidden_size, config.text_config.hidden_size, bias=True)
        self.act = ACT2FN[config.projector_hidden_act]
        self.linear_2 = nn.Linear(config.text_config.hidden_size, config.text_config.hidden_size, bias=True)

    def forward(self, time_series_features, input_mask):
        masked_features = time_series_features * input_mask.unsqueeze(-1) + self.mask_embedding * (1 - input_mask.unsqueeze(-1))
        hidden_states = self.linear_1(masked_features)
        hidden_states = self.act(hidden_states)
        hidden_states = self.linear_2(hidden_states)
        return hidden_states
    

class MistsPreTrainedModel(PreTrainedModel):
    config_class = MistsConfig
    base_model_prefix = "model"
    supports_gradient_checkpointing = True
    _no_split_modules = ["T5Block"]
    _skip_keys_device_placement = "past_key_values"
    _supports_flash_attn_2 = True
    _supports_sdpa = True
    _supports_cache_class = True
    _supports_static_cache = True

    def _init_weights(self, module):
        # important: 現状Mistralの初期化コードをそのまま移植している。
        # refers: https://github.com/huggingface/transformers/blob/25245ec26dc29bcf6102e1b4ddd0dfd02e720cf5/src/transformers/models/mistral/modeling_mistral.py#L762
        # 現状のまま事前学習を行うのは望ましくなく、FineTuningと推論のみが可能。
        std = self.config.text_config.initializer_range
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()


class MistsForConditionalGeneration(MistsPreTrainedModel):
    def __init__(self, config: MistsConfig):
        super().__init__(config)

        self.time_series_tower = MomentEmbeddingModel(config.time_series_config)
        self.multi_modal_projector = MistsMultiModalProjector(config)
        self.vocab_size = config.text_config.vocab_size
        self.language_model = AutoModelForCausalLM.from_config(
            config.text_config, attn_implementation=config._attn_implementation
        )
        self.pad_token_id = self.config.pad_token_id if self.config.pad_token_id is not None else -1
        self.post_init()

    def get_time_series_tower(self):
        time_series_tower = getattr(self, 'time_series_tower', None)
        if type(time_series_tower) is list:
            time_series_tower = time_series_tower[0]
        return time_series_tower

    def get_input_embeddings(self):
        return self.language_model.get_input_embeddings()

    def set_input_embeddings(self, value):
        self.language_model.set_input_embeddings(value)

    def get_output_embeddings(self):
        return self.language_model.get_output_embeddings()

    def set_output_embeddings(self, new_embeddings):
        self.language_model.set_output_embeddings(new_embeddings)

    def set_decoder(self, decoder):
        self.language_model.set_decoder(decoder)

    def get_decoder(self):
        return self.language_model.get_decoder()

    def tie_weights(self):
        return self.language_model.tie_weights()

    def resize_token_embeddings(self, new_num_tokens: Optional[int] = None, pad_to_multiple_of=None) -> nn.Embedding:
        model_embeds = self.language_model.resize_token_embeddings(new_num_tokens, pad_to_multiple_of)
        # update vocab size
        self.config.text_config.vocab_size = model_embeds.num_embeddings
        self.vocab_size = model_embeds.num_embeddings
        return model_embeds
    
    # copy _merge_input_ids_with_image_features from LlabaForConditionalGeneration
    # refers: https://github.com/huggingface/transformers/blob/25245ec26dc29bcf6102e1b4ddd0dfd02e720cf5/src/transformers/models/llava/modeling_llava.py#L277C9-L277C45
    def _merge_input_ids_with_time_series_features(self, time_series_features, inputs_embeds, input_ids, attention_mask, labels):
        num_time_series, num_time_series_patches, embed_dim = time_series_features.shape  # num_time_series_patches = n_channels x n_patches
        batch_size, sequence_length = input_ids.shape
        left_padding = not torch.sum(input_ids[:, -1] == torch.tensor(self.pad_token_id))
        # 1. Create a mask to know where special time_series tokens are
        special_time_series_token_mask = input_ids == self.config.time_series_token_index
        num_special_time_series_tokens = torch.sum(special_time_series_token_mask, dim=-1)
        # Compute the maximum embed dimension
        max_embed_dim = (num_special_time_series_tokens.max() * (num_time_series_patches - 1)) + sequence_length
        max_embed_dim = int(max_embed_dim.item())  # テンソルから整数値を取得
        batch_indices, non_time_series_indices = torch.where(input_ids != self.config.time_series_token_index)

        # 2. Compute the positions where text should be written
        # Calculate new positions for text tokens in merged time_series-text sequence.
        # `special_time_series_token_mask` identifies time_series tokens. Each time_series token will be replaced by `nb_text_tokens_per_time_series - 1` text tokens.
        # `torch.cumsum` computes how each time_series token shifts subsequent text token positions.
        # - 1 to adjust for zero-based indexing, as `cumsum` inherently increases indices by one.
        new_token_positions = torch.cumsum((special_time_series_token_mask * (num_time_series_patches - 1) + 1), -1) - 1
        nb_time_series_pad = max_embed_dim - 1 - new_token_positions[:, -1]
        if left_padding:
            new_token_positions += nb_time_series_pad[:, None]  # offset for left padding
        text_to_overwrite = new_token_positions[batch_indices, non_time_series_indices]

        # 3. Create the full embedding, already padded to the maximum position
        final_embedding = torch.zeros(
            batch_size, max_embed_dim, embed_dim, dtype=inputs_embeds.dtype, device=inputs_embeds.device
        )
        final_attention_mask = torch.zeros(
            batch_size, max_embed_dim, dtype=attention_mask.dtype, device=inputs_embeds.device
        )
        if labels is not None:
            final_labels = torch.full(
                (batch_size, max_embed_dim), self.config.ignore_index, dtype=input_ids.dtype, device=input_ids.device
            )
        # In case the Vision model or the Language model has been offloaded to CPU, we need to manually
        # set the corresponding tensors into their correct target device.
        target_device = inputs_embeds.device
        batch_indices, non_time_series_indices, text_to_overwrite = (
            batch_indices.to(target_device),
            non_time_series_indices.to(target_device),
            text_to_overwrite.to(target_device),
        )
        attention_mask = attention_mask.to(target_device)

        # 4. Fill the embeddings based on the mask. If we have ["hey" "<time_series>", "how", "are"]
        # we need to index copy on [0, 577, 578, 579] for the text and [1:576] for the time_series features
        final_embedding[batch_indices, text_to_overwrite] = inputs_embeds[batch_indices, non_time_series_indices]
        final_attention_mask[batch_indices, text_to_overwrite] = attention_mask[batch_indices, non_time_series_indices]
        if labels is not None:
            final_labels[batch_indices, text_to_overwrite] = labels[batch_indices, non_time_series_indices]

        # 5. Fill the embeddings corresponding to the time_series. Anything that is not `text_positions` needs filling (#29835)
        time_series_to_overwrite = torch.full(
            (batch_size, max_embed_dim), True, dtype=torch.bool, device=inputs_embeds.device
        )
        time_series_to_overwrite[batch_indices, text_to_overwrite] = False
        time_series_to_overwrite &= time_series_to_overwrite.cumsum(-1) - 1 >= nb_time_series_pad[:, None].to(target_device)

        if time_series_to_overwrite.sum() != time_series_features.shape[:-1].numel():
            raise ValueError(
                f"The input provided to the model are wrong. The number of time series tokens is {torch.sum(special_time_series_token_mask)} while"
                f" the number of time series given to the model is {num_time_series}. This prevents correct indexing and breaks batch generation."
            )

        final_embedding[time_series_to_overwrite] = time_series_features.contiguous().reshape(-1, embed_dim).to(target_device)
        final_attention_mask |= time_series_to_overwrite
        position_ids = (final_attention_mask.cumsum(-1) - 1).masked_fill_((final_attention_mask == 0), 1)

        # 6. Mask out the embedding at padding positions, as we later use the past_key_value value to determine the non-attended tokens.
        batch_indices, pad_indices = torch.where(input_ids == self.pad_token_id)
        indices_to_mask = new_token_positions[batch_indices, pad_indices]

        final_embedding[batch_indices, indices_to_mask] = 0

        if labels is None:
            final_labels = None

        return final_embedding, final_attention_mask, final_labels, position_ids
    
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        time_series_values: torch.FloatTensor = None,
        time_series_input_mask: torch.FloatTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        # time_series_feature_layer: Optional[int] = None,
        # time_series_feature_select_strategy: Optional[str] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, MistsCausalLMOutputWithPast]:
        
        # language_modelの引数で変わる
        # output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        # output_hidden_states = (
        #     output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        # )
        # return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        # vision_feature_layer = (
        #     vision_feature_layer if vision_feature_layer is not None else self.config.vision_feature_layer
        # )
        # vision_feature_select_strategy = (
        #     vision_feature_select_strategy
        #     if vision_feature_select_strategy is not None
        #     else self.config.vision_feature_select_strategy
        # )

        if inputs_embeds is None:
            # 1. Extra the input embeddings
            inputs_embeds = self.get_input_embeddings()(input_ids)

            # 2. Merge text and time_series
            if time_series_values is not None and input_ids.shape[1] != 1:
                time_series_outputs = self.time_series_tower(time_series_values, time_series_input_mask)
                time_series_features = self.multi_modal_projector(
                    time_series_features=time_series_outputs.hidden_states,  # [batch_size, n_patches, d_model]
                    input_mask=time_series_outputs.input_mask_patch_view,    # [batch_size, n_paches]
                )

                inputs_embeds = inputs_embeds.to(time_series_features.dtype)
                inputs_embeds, attention_mask, labels, position_ids =self._merge_input_ids_with_time_series_features(
                    time_series_features, inputs_embeds, input_ids, attention_mask, labels
                )

            # In case input_ids.shape[1] == 1 & time_series_values==None & past_key_values != None, we are in the case of
            # generation with cache
            elif past_key_values is not None and time_series_values is not None and input_ids.shape[1] == 1:
                # Retrieve the first layer to inspect the logits and mask out the hidden states
                # that are set to 0
                first_layer_past_key_value = past_key_values[0][0][:, :, :, 0]

                # Sum all dimensions of head_dim (-2) to avoid random errors such as: https://github.com/huggingface/transformers/pull/28032#issuecomment-1863691941
                batch_index, non_attended_tokens = torch.where(first_layer_past_key_value.float().sum(-2) == 0)

                # Get the target length
                target_length = input_ids.shape[1]
                past_length = first_layer_past_key_value.shape[-1]

                extended_attention_mask = torch.ones(
                    (attention_mask.shape[0], past_length),
                    dtype=attention_mask.dtype,
                    device=attention_mask.device,
                )

                # Filter out only the tokens that can be un-attended, this can happen
                # if one uses Llava + Fused modules where the cache on the
                # first iteration is already big enough, or if one passes custom cache
                valid_indices = non_attended_tokens < extended_attention_mask.size(-1)
                new_batch_index = batch_index[valid_indices]
                new_non_attended_tokens = non_attended_tokens[valid_indices]

                # Zero-out the places where we don't need to attend
                extended_attention_mask[new_batch_index, new_non_attended_tokens] = 0

                attention_mask = torch.cat((extended_attention_mask, attention_mask[:, -target_length:]), dim=1)
                position_ids = torch.sum(attention_mask, dim=1).unsqueeze(-1) - 1
            
        outputs = self.language_model(
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds.to(self.language_model.dtype),
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        logits = outputs[0]

        loss = None
        if labels is not None:
            # Shift so that tokens < n predict n
            if attention_mask is not None:
                shift_attention_mask = attention_mask[..., 1:]
                shift_logits = logits[..., :-1, :][shift_attention_mask.to(logits.device) != 0].contiguous()
                shift_labels = labels[..., 1:][shift_attention_mask.to(labels.device) != 0].contiguous()
            else:
                shift_logits = logits[..., :-1, :].contiguous()
                shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = nn.CrossEntropyLoss()
            loss = loss_fct(
                shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1).to(shift_logits.device)
            )

        if not return_dict:
            output = (logits,) + outputs[1:]
            return (loss,) + output if loss is not None else output
        
        return MistsCausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )
    
    def prepare_inputs_for_generation(
        self, input_ids, past_key_values=None, inputs_embeds=None, time_series_values=None, attention_mask=None, **kwargs
    ):
        if past_key_values is not None:
            if isinstance(past_key_values, Cache):
                cache_length = past_key_values.get_seq_length()
                past_length = past_key_values.seen_tokens
            else:
                cache_length = past_length = past_key_values[0][0].shape[2]

            # Keep only the unprocessed tokens:
            # 1 - If the length of the attention_mask exceeds the length of input_ids, then we are in a setting where
            # some of the inputs are exclusively passed as part of the cache (e.g. when passing input_embeds as
            # input)
            if attention_mask is not None and attention_mask.shape[1] > input_ids.shape[1]:
                input_ids = input_ids[:, -(attention_mask.shape[1] - past_length) :]
            # 2 - If the past_length is smaller than input_ids', then input_ids holds all input tokens. We can discard
            # input_ids based on the past_length.
            elif past_length < input_ids.shape[1]:
                input_ids = input_ids[:, past_length:]
            # 3 - Otherwise (past_length >= input_ids.shape[1]), let's assume input_ids only has unprocessed tokens.
            elif self.config.time_series_token_index in input_ids:
                input_ids = input_ids[:, input_ids.shape[1] - 1 :]
            # If the cache has seen more tokens than it can hold, then the cache has a size limit. Let's discard the
            # older attention values, as their corresponding values are not part of the input.
            if cache_length < past_length and attention_mask is not None:
                attention_mask = attention_mask[:, -(cache_length + input_ids.shape[1]) :]

        position_ids = kwargs.get("position_ids", None)
        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.long().cumsum(-1) - 1
            position_ids.masked_fill_(attention_mask == 0, 1)
            if past_key_values:
                position_ids = position_ids[:, -input_ids.shape[1] :]

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs.update(
            {
                "position_ids": position_ids,
                "past_key_values": past_key_values,
                "use_cache": kwargs.get("use_cache"),
                "attention_mask": attention_mask,
                "time_series_values": time_series_values,
            }
        )
        return model_inputs
    
    def _reorder_cache(self, *args, **kwargs):
        return self.language_model._reorder_cache(*args, **kwargs)




