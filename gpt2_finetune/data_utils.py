import logging
import numpy as np
import random

MAX_ROBERTA_LENGTH = 502

random.seed(12)
logger = logging.getLogger(__name__)


class Instance(object):
    def __init__(self, args, config, instance_dict):
        self.dict = instance_dict
        self.args = args
        self.config = config
        self.truncated = False
        self.sent1_tokens = np.array(instance_dict["sent1_tokens"], dtype=np.int32)
        self.sent1_tokens_tags = None
        self.sent2_tokens = np.array(instance_dict["sent2_tokens"], dtype=np.int32)
        self.init_context_size = config["max_prefix_length"] + 1

    def preprocess(self, tokenizer):
        # shorten the very long sequences in the instance based on DATASET_CONFIG
        self.truncate()
        # whenever args.context_input_type has "_shuffle_" or "_reverse_"
        # exchange prefix/suffix with 50% probability or 100% probability
        self.shuffle_prefix_suffix()
        # Finally, perform prefix and suffix padding to build the sentence, label and segments
        self.build_sentence(tokenizer)
        self.build_label(tokenizer)
        self.build_segment(tokenizer)
        # check if the padding worked out correctly and all the lengths are aligned
        self.check_constraints()

    def truncate(self):
        config = self.config
        max_prefix_length = config["max_prefix_length"]
        max_suffix_length = config["max_suffix_length"]
        if len(self.sent1_tokens) > max_prefix_length:
            self.truncated = True
            self.sent1_tokens = self.sent1_tokens[:max_prefix_length]
        if len(self.sent2_tokens) > max_suffix_length:
            self.truncated = True
            self.sent2_tokens = self.sent2_tokens[:max_suffix_length]

    def shuffle_prefix_suffix(self):
        if "_shuffle_" in self.args.context_input_type:
            # shuffle with 50% probability
            if random.random() <= 0.5:
                self.sent1_tokens, self.sent2_tokens = self.sent2_tokens, self.sent1_tokens

        elif "_reverse_" in self.args.context_input_type:
            self.sent1_tokens, self.sent2_tokens = self.sent2_tokens, self.sent1_tokens

    def build_sentence(self, tokenizer):
        if self.args.context_input_type.endswith("_no_srl_input"):
            self.sent_prefix = np.array([], dtype=np.int64)
        else:
            self.sent_prefix = left_padding(
                self.sent1_tokens, tokenizer.pad_token_id, self.config["max_prefix_length"]
            )

        self.sent_suffix = right_padding(
            np.append(self.sent2_tokens, tokenizer.eos_token_id),
            tokenizer.pad_token_id,
            self.config["max_suffix_length"] + 1
        )
        self.sentence = np.concatenate(
            [self.sent_prefix, [tokenizer.bos_token_id], self.sent_suffix]
        )

    def build_label(self, tokenizer):
        dense_length = self.config["global_dense_length"] + self.config["roberta_dense_length"]
        self.label_suffix = right_padding(
            np.append(self.sent2_tokens, tokenizer.eos_token_id),
            -1,
            self.config["max_suffix_length"] + 1
        )
        self.label = np.concatenate([
            [-1 for _ in range(dense_length)],
            [-1 for _ in self.sent_prefix],
            [-1],
            self.label_suffix
        ]).astype(np.int64)

    def build_segment(self, tokenizer):
        dense_length = self.config["global_dense_length"] + self.config["roberta_dense_length"]
        if self.sent1_tokens_tags is not None:
            prefix_segment = left_padding(
                tokenizer.convert_tokens_to_ids(self.sent1_tokens_tags),
                tokenizer.pad_token_id,
                self.config["max_prefix_length"]
            )
            suffix_segment_tag = tokenizer.additional_special_tokens_ids[1]
        else:
            prefix_segment = [tokenizer.additional_special_tokens_ids[1] for _ in self.sent_prefix]
            suffix_segment_tag = tokenizer.additional_special_tokens_ids[2]

        self.segment = np.concatenate([
            [tokenizer.additional_special_tokens_ids[0] for _ in range(dense_length)],
            prefix_segment,
            [suffix_segment_tag],
            [suffix_segment_tag for _ in self.sent_suffix],
        ]).astype(np.int64)

    def check_constraints(self):
        dense_length = self.config["global_dense_length"] + self.config["roberta_dense_length"]
        assert len(self.sentence) == len(self.label) - dense_length
        assert len(self.sentence) == len(self.segment) - dense_length


class HPInstance(Instance):
    def __init__(self, args, config, instance_dict):
        self.dict = instance_dict
        self.args = args
        self.config = config
        self.truncated = False
        self.init_context_size = config["max_prefix_length"] + 1

        self.original_sentence = instance_dict["sentence"]
        self.roberta_sentence = instance_dict["roberta_sentence"]
        self.author_tag_str = instance_dict["author_tag_str"]
        self.author_tag_ids = instance_dict["author_tag_ids"]
        self.author_target = instance_dict["author_target"]
        self.original_author_target = instance_dict["original_target"]

        if "roberta_author_target" not in instance_dict:
            self.roberta_author_target = instance_dict["author_target"]
        else:
            self.roberta_author_target = instance_dict["roberta_author_target"]

        # Choose how to build sent1_tokens and sent2_tokens
        self.decide_sent1_tokens()
        self.sent2_tokens = np.array(self.original_sentence, dtype=np.int32)

        # process RoBERTa separately since it relies on fairseq
        self.preprocess_roberta()

    def preprocess_roberta(self):
        roberta_sentence = self.roberta_sentence

        if isinstance(roberta_sentence, str):
            roberta_sentence = np.array(
                [int(x) for x in roberta_sentence.split()],
                dtype=np.int32
            )
        else:
            roberta_sentence = roberta_sentence.numpy().astype(np.int32)

        # Pad or truncate the RoBERTa sentence length to MAX_ROBERTA_LENGTH
        if len(roberta_sentence) < MAX_ROBERTA_LENGTH:
            roberta_sentence = right_padding(roberta_sentence, 1, MAX_ROBERTA_LENGTH)
        else:
            roberta_sentence = roberta_sentence[:MAX_ROBERTA_LENGTH]

        assert len(roberta_sentence) == MAX_ROBERTA_LENGTH
        self.roberta_sentence = roberta_sentence

    def decide_sent1_tokens(self):
        args = self.args
        if args.context_input_type.endswith("_no_srl_input"):
            self.sent1_tokens = np.array([])
            self.sent1_tokens_tags = None
        elif args.context_input_type.endswith("_roberta_input"):
            self.sent1_tokens = np.array(
                [int(x) for x in self.roberta_sentence.split()],
                dtype=np.int32
            )
            self.sent1_tokens_tags = None
        else:
            self.sent1_tokens = np.array(
                [int(x) for x in self.author_tag_str],
                dtype=np.int32
            )
            self.sent1_tokens_tags = self.author_tag_ids

def np_prepend(array, value):
    return np.insert(array, 0, value)

def left_padding(data, pad_token, total_length):
    tokens_to_pad = total_length - len(data)
    return np.pad(data, (tokens_to_pad, 0), constant_values=pad_token)

def right_padding(data, pad_token, total_length):
    tokens_to_pad = total_length - len(data)
    return np.pad(data, (0, tokens_to_pad), constant_values=pad_token)

def string_to_ids(text, tokenizer):
    return tokenizer.convert_tokens_to_ids(tokenizer.tokenize(text))

def limit_dataset_size(dataset, limit_examples):
    """Limit the dataset size to a small number for debugging / generation."""

    if limit_examples:
        logger.info("Limiting dataset to {:d} examples".format(limit_examples))
        dataset = dataset[:limit_examples]

    return dataset

def limit_authors(dataset, specific_author_train, split, reverse_author_target_dict):
    """Limit the dataset size to a certain author."""
    specific_author_train = [int(x) for x in specific_author_train.split(",")]

    original_dataset_size = len(dataset)
    if split in ["train", "test"] and -1 not in specific_author_train:
        logger.info(
            "Preserving authors = {}".format(", ".join([reverse_author_target_dict[x] for x in specific_author_train]))
        )
        dataset = [
            x for x in dataset if x["author_target"] in specific_author_train
        ]
        logger.info("Remaining instances after author filtering = {:d} / {:d}".format(len(dataset), original_dataset_size))
    return dataset


def datum_to_dict(config, datum, tokenizer):
    """Convert an datum to the instance dictionary."""

    instance_dict = {"metadata": ""}

    for key in config["keys"]:
        element_value = datum[key["position"]]
        instance_dict[key["key"]] = string_to_ids(element_value, tokenizer) if key["tokenize"] else element_value
        if key["metadata"]:
            instance_dict["metadata"] += "%s = %s, " % (key["key"], str(element_value))

    instance_dict["metadata"] = instance_dict["metadata"][:-2]
    return instance_dict


def aggregate_content_information(content_vectors, content_aggregation):
    # select vectors at every content_aggregation interval
    content_agg_indices = [i for i in range(content_vectors.shape[1]) if i % content_aggregation == 0]
    content_agg_vectors = content_vectors[:, content_agg_indices, :]
    return content_agg_vectors

def update_config(args, config):
    if args.context_input_type.endswith("_no_srl_input"):
        config["max_prefix_length"] = 0

    if args.global_dense_feature_list != "none":
        global_dense_length = len(args.global_dense_feature_list.split(","))
    else:
        global_dense_length = 0

    if args.content_aggregation > MAX_ROBERTA_LENGTH:
        roberta_dense_length = 0
    else:
        roberta_dense_length = len(
            [i for i in range(MAX_ROBERTA_LENGTH - 1) if i % args.content_aggregation == 0]
        )

    if global_dense_length > 0:
        logger.info("Using {:d} dense feature vectors.".format(global_dense_length))

    if roberta_dense_length > 0:
        logger.info("Using {:d} roberta feature vectors.".format(roberta_dense_length))

    assert global_dense_length <= config["max_dense_length"]

    config["global_dense_length"] = global_dense_length
    config["roberta_dense_length"] = roberta_dense_length