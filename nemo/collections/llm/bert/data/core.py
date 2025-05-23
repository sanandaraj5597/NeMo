# Copyright (c) 2025, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path
from random import choices, sample
from typing import Literal, Mapping, Optional

import datasets
import numpy as np
import torch

from nemo.collections.common.tokenizers import TokenizerSpec
from nemo.collections.llm.gpt.data.utils import _get_samples_mapping, _JSONLMemMapDataset
from nemo.core.classes import Dataset
from nemo.lightning.base import NEMO_DATASETS_CACHE
from nemo.utils import logging

# hack to avoid the "not enough disk space" error in some slurm cluster
datasets.builder.has_sufficient_disk_space = lambda needed_bytes, directory='.': True


def get_dataset_root(name: str) -> Path:
    """Retrieve the root path for the dataset. Create the folder if not exists."""
    output = Path(NEMO_DATASETS_CACHE) / name
    output.mkdir(parents=True, exist_ok=True)

    return output


def create_sft_dataset(
    path: Path,
    tokenizer: "TokenizerSpec",
    seq_length: int = 2048,
    add_bos: bool = False,
    add_eos: bool = True,
    seed: int = 1234,
    index_mapping_dir: Optional[str] = None,
    truncation_method: str = 'right',
    memmap_workers: int = 2,
    data_type: str = 'train',
    num_hard_negatives: int = 1,
    **kwargs,
) -> "BertEmbeddingDataset":
    """Create BertEmbeddingDataset for SFT training."""

    return BertEmbeddingDataset(
        file_path=str(path),
        tokenizer=tokenizer,
        max_seq_length=seq_length,
        add_bos=add_bos,
        add_eos=add_eos,
        memmap_workers=memmap_workers,
        seed=seed,
        index_mapping_dir=index_mapping_dir,
        truncation_method=truncation_method,
        data_type=data_type,
        num_hard_negatives=num_hard_negatives,
        **kwargs,
    )


class BertEmbeddingDataset(Dataset):
    """ """

    def __init__(
        self,
        file_path: str,
        tokenizer: TokenizerSpec,
        max_seq_length: int = 1024,
        min_seq_length: int = 1,
        add_bos: bool = True,
        add_eos: bool = True,
        max_num_samples: int = None,
        seed: int = 1234,
        index_mapping_dir: str = None,
        virtual_tokens: int = 0,
        memmap_workers: Optional[int] = None,
        truncation_method: str = 'right',
        special_tokens: Optional[Mapping[str, str]] = None,  # special tokens, a dictory of {token_type: token}
        data_type: str = 'train',  # train, query or doc
        num_hard_negatives: int = 4,
        negative_sample_strategy: Literal["random", "first"] = 'first',
    ):
        """
        file_path: Path to a JSONL dataset with (query,pos_doc,neg_doc) triplets in jsonl format.
        tokenizer: Tokenizer for the dataset. Instance of a class that inherits TokenizerSpec.
        max_seq_length (int): maximum sequence length for each dataset examples.
            Examples will either be truncated to fit this length or dropped if they cannot be truncated.
        min_seq_length (int): min length of each data example in the dataset.
            Data examples will be dropped if they do not meet the min length requirements.
        add_bos (bool): Whether to add a beginning of sentence token to each data example
        add_eos (bool): Whether to add an end of sentence token to each data example
        seed: Random seed for data shuffling.
        max_num_samples: Maximum number of samples to load.
            This can be > dataset length if you want to oversample data. If None, all samples will be loaded.
        index_mapping_dir: Directory to save the index mapping to.
            If None, will write to the same folder as the dataset.
        truncation_method: Truncation from which position. Options: ['left', 'right']
        special_tokens: special tokens for the chat prompts, a dictionary of {token_type: token}.
            Default: {
                        'system_turn_start': '<extra_id_0>',
                        'turn_start': '<extra_id_1>',
                        'label_start': '<extra_id_2>',
                        'end_of_turn': '\n',
                        'end_of_name": '\n'
                    }
        negative_sample_strategy: Strategy for negative samples. Options: ['random', 'first']
        """
        # TODO: lot of copy-paste from GPTSFDDataset, should refactor both to use a common base class (@adithyare)
        self.tokenizer = tokenizer
        self.file_path = file_path
        self.max_seq_length = max_seq_length
        self.min_seq_length = min_seq_length
        self.add_bos = add_bos
        self.add_eos = add_eos
        self.max_num_samples = max_num_samples
        self.seed = seed
        self.index_mapping_dir = index_mapping_dir
        self.virtual_tokens = virtual_tokens
        self.truncation_method = truncation_method
        self.pad_token_id = self.tokenizer.pad_id if self.tokenizer.pad_id else self.tokenizer.eos_id
        self.negative_sample_strategy = negative_sample_strategy
        assert (
            truncation_method == 'left' or truncation_method == 'right'
        ), 'truncation_method must be either "left" or "right"'
        assert (
            negative_sample_strategy == 'random' or negative_sample_strategy == 'first'
        ), 'negative_sample_strategy must be either "random" or "first"'
        if special_tokens is None:
            self.special_tokens = {
                "system_turn_start": "<extra_id_0>",
                "turn_start": "<extra_id_1>",
                "label_start": "<extra_id_2>",
                "end_of_turn": "\n",
                "end_of_name": "\n",
            }
        else:
            self.special_tokens = special_tokens
        self.data_type = data_type
        self.num_hard_negatives = num_hard_negatives

        self.indexed_dataset = _JSONLMemMapDataset(
            dataset_paths=[file_path],
            tokenizer=None,
            header_lines=0,
            index_mapping_dir=index_mapping_dir,
            workers=memmap_workers,
        )
        # Will be None after this call if `max_num_samples` is None
        self.samples_mapping = None
        self._build_samples_mapping()
        logging.info(
            f"Creating EmbeddingDataset with seed={self.seed},\n"
            f"add_bos={self.add_bos}, add_eos={self.add_eos},\n"
            f"max_seq_length={self.max_seq_length}, min_seq_length={self.min_seq_length},\n"
            f"pad_token_id={self.pad_token_id}, negative_sample_strategy={self.negative_sample_strategy},\n"
            f"num_hard_negatives={self.num_hard_negatives}."
        )

    def _build_samples_mapping(self):
        if self.max_num_samples is not None:
            self.samples_mapping = _get_samples_mapping(
                indexed_dataset=self.indexed_dataset,
                data_prefix=self.file_path,
                num_epochs=None,
                max_num_samples=self.max_num_samples,
                max_seq_length=self.max_seq_length - 2,
                short_seq_prob=0,
                seed=self.seed,
                name=self.file_path.split('/')[-1],
                binary_head=False,
                index_mapping_dir=self.index_mapping_dir,
            )
        else:
            self.samples_mapping = None

    def __len__(self):
        if self.max_num_samples is None:
            return len(self.indexed_dataset)
        else:
            assert self.samples_mapping is not None
            return len(self.samples_mapping)

    def __getitem__(self, idx):
        if isinstance(idx, np.int64):
            idx = idx.item()

        if self.samples_mapping is not None:
            assert idx < len(self.samples_mapping)
            idx, _, _ = self.samples_mapping[idx]
            if isinstance(idx, np.uint32):
                idx = idx.item()

        if idx is not None:
            assert idx < len(self.indexed_dataset)
        else:
            idx = -1
        # idx may < 0 because we pad_samples_to_global_batch_size, e.g. id = -1
        if idx < 0:
            idx = len(self) + idx
            auto_gen_idx = True
        else:
            auto_gen_idx = False
        try:
            example = self.indexed_dataset[idx]
            if auto_gen_idx:
                example['__AUTOGENERATED__'] = True
        except Exception as e:
            logging.error(f"Error while loading example {idx} from dataset {self.file_path}")
            raise e
        return self._process_example(example)

    def _process_example(self, example):
        """
        Create an example by concatenating text and answer.
        Truncation is carried out when needed, but it is performed only on the prompt side.
        BOS, EOS, and SEP, are added if specified.
        """

        metadata = {k: v for k, v in example.items()}
        if self.data_type == 'train':
            q = self.tokenizer.text_to_ids("query: " + example['query'].strip())
            d = self.tokenizer.text_to_ids("passage: " + example['pos_doc'].strip())
            # handle cases where the required number of hard negatives are not present
            if len(example['neg_doc']) < self.num_hard_negatives:
                nd = example['neg_doc']
                # sample rest with replacement
                nd = nd + choices(example['neg_doc'], k=self.num_hard_negatives - len(example['neg_doc']))
            else:
                if self.negative_sample_strategy == 'random':
                    # sample without replacement
                    # Choose the first self.num_hard_negatives
                    nd = sample(example['neg_doc'], k=self.num_hard_negatives)
                else:
                    # Choose the first self.num_hard_negatives samples
                    nd = example['neg_doc'][: self.num_hard_negatives]
            assert len(nd) == self.num_hard_negatives, "Error in sampling required number of hard negatives"
            nd = [self.tokenizer.text_to_ids("passage: " + ex.strip()) for ex in nd]

        elif self.data_type == 'query':
            q = self.tokenizer.text_to_ids("query: " + example['query'].strip())
            d, nd = None, None
            assert "query_id" in example, "query_id is required for query dataset"
            assert "doc_id" in example, "doc_id is required for query dataset"
        elif self.data_type == 'doc':
            d = self.tokenizer.text_to_ids("passage: " + example['pos_doc'].strip())
            assert "doc_id" in example, "doc_id is required for doc dataset"
            q, nd = None, None
        else:
            raise ValueError(f"Invalid data type: {self.data_type}")

        q = q if q is not None else []
        d = d if d is not None else []
        nd = nd if nd is not None else []

        if self.virtual_tokens:
            # (@adithyare) we are going to insert "pad/eos" tokens in the beginning of the text and context
            # these pad/eos tokens are placeholders for virtual tokens for ptuning (if used)
            q = [self.tokenizer.eos_id] * self.virtual_tokens + q  # type: ignore
            d = [self.tokenizer.eos_id] * self.virtual_tokens + d  # type: ignore
            nd = [[self.tokenizer.eos_id] * self.virtual_tokens + n for n in nd]  # type: ignore

        if self.add_bos:
            q = [self.tokenizer.bos_id] + q  # type: ignore
            d = [self.tokenizer.bos_id] + d  # type: ignore
            nd = [[self.tokenizer.bos_id] + n for n in nd]  # type: ignore

        # TODO: (@adithyare) should probably add a warning before truncation
        q = q[: self.max_seq_length - 1]
        d = d[: self.max_seq_length - 1]
        nd = [n[: self.max_seq_length - 1] for n in nd]

        if self.add_eos:
            q = q + [self.tokenizer.eos_id]  # type: ignore
            d = d + [self.tokenizer.eos_id]  # type: ignore
            nd = [n + [self.tokenizer.eos_id] for n in nd]  # type: ignore

        processed_example = {
            'query': q,
            'pos_doc': d,
            'neg_doc': nd,
            'metadata': metadata,
        }
        return processed_example

    def _maybe_cast_to_list(self, x):
        if isinstance(x, np.ndarray):
            return [item.tolist() for item in x]
        return x

    def _ceil_to_nearest(self, n, m):
        return (n + m - 1) // m * m

    def _collate_item(self, item, max_length):
        item = self._maybe_cast_to_list(item)
        pad_id = self.pad_token_id
        if self.truncation_method == 'left':
            item = [[pad_id] * (max_length - len(x)) + x for x in item]
        else:
            item = [x + [pad_id] * (max_length - len(x)) for x in item]
        return item

    @torch.no_grad()
    def _create_attention_mask2(self, max_length, item_length):
        """Create `attention_mask`.
        Args:
            input_ids: A 1D tensor that holds the indices of tokens.
        """
        # seq_length = len(input_ids)
        # `attention_mask` has the shape of [1, seq_length, seq_length]
        attention_mask = torch.zeros(max_length)
        if self.truncation_method == 'left':
            # input ids:      [pad] [pad] token token |
            # attention mask: 0      0    1     1
            attention_mask[max_length - item_length :] = 1
        else:
            # input ids:      token token [pad] [pad] |
            # attention mask: 1     1     0      0
            attention_mask[:item_length] = 1
        return attention_mask

    def _collate_fn(self, batch):
        """
        Collate query passage together
        """
        input_ids = []
        metadata = []
        lengths = []
        max_length = -1
        for item in batch:
            metadata.append(item['metadata'])
            if self.data_type == 'train':
                input_ids.append(item['query'])
                lengths.append(len(item['query']))
                input_ids.append(item['pos_doc'])
                lengths.append(len(item['pos_doc']))
                for nd in item['neg_doc']:
                    input_ids.append(nd)
                    lengths.append(len(nd))
                max_length = max(
                    max_length, len(item['query']), len(item['pos_doc']), *(len(nd) for nd in item['neg_doc'])
                )
            elif self.data_type == 'query':
                input_ids.append(item['query'])
                lengths.append(len(item['query']))
                max_length = max(max_length, len(item['query']))
            elif self.data_type == 'doc':
                input_ids.append(item['pos_doc'])
                lengths.append(len(item['pos_doc']))
                max_length = max(max_length, len(item['pos_doc']))
            else:
                raise ValueError(f"Invalid data type: {self.data_type}")

        max_length = min(self.max_seq_length, self._ceil_to_nearest(max_length, 16))
        assert max_length <= self.max_seq_length

        attention_mask = [self._create_attention_mask2(max_length, len) for len in lengths]
        attention_mask = torch.stack(attention_mask)
        position_ids = [list(range(max_length)) for _ in batch]
        position_ids = torch.LongTensor(position_ids)
        input_ids = torch.LongTensor(self._collate_item(input_ids, max_length=max_length))
        lengths = torch.LongTensor(lengths) - 1  # subtract 1 to account for the eos token

        processed_batch = {
            'input_ids': input_ids,
            'token_type_ids': torch.zeros_like(input_ids),
            'attention_mask': attention_mask,
            'metadata': metadata,
            'position_ids': position_ids,
        }

        return processed_batch
