import os
import re
from multiprocessing.dummy import Pool
import rouge
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from typing import NamedTuple, List, Callable, Dict, Tuple, Optional
from collections import Counter
from random import shuffle
from functools import lru_cache
import torch
import json

plt.switch_backend('agg')
word_detector = re.compile('\w')
non_word_char_in_word = re.compile(r"(?<=\w)\W(?=\w)")
not_for_output = {'<PAD>', '<SOS>', '<EOS>', '<UNK>'}
evaluator = rouge.Rouge(metrics=['rouge-l'])


class Vocab(object):
    PAD = 0
    SOS = 1
    EOS = 2
    UNK = 3

    def __init__(self):
        self.word2index = {}
        self.word2count = Counter()
        self.reserved = ['<PAD>', '<SOS>', '<EOS>', '<UNK>']
        self.index2word = self.reserved[:]
        self.embeddings = None

    def add_words(self, words: List[str]):
        for word in words:
            if word not in self.word2index:
                self.word2index[word] = len(self.index2word)
                self.index2word.append(word)
        self.word2count.update(words)

    def trim(self, *, vocab_size: int = None, min_freq: int = 4):
        if min_freq <= 1 and (vocab_size is None or vocab_size >= len(self.word2index)):
            return
        ordered_words = sorted(((c, w) for (w, c) in self.word2count.items()), reverse=True)
        if vocab_size:
            ordered_words = ordered_words[:vocab_size]
        self.word2index = {}
        self.word2count = Counter()
        self.index2word = self.reserved[:]
        for count, word in ordered_words:
            if count < min_freq: break
            self.word2index[word] = len(self.index2word)
            self.word2count[word] = count
            self.index2word.append(word)

    def load_embeddings(self, embed_path: str, dtype=np.float32) -> int:
        load_num = 0
        vocab_size = len(self)
        with open(embed_path, 'r') as f:
            for line in f.readlines():
                data = line.strip().split()
                word = data[0]
                data.pop(0)
                idx = self.word2index.get(word, None)  # word -> id
                if idx is not None:
                    vec = np.array(data, dtype=dtype)
                    if self.embeddings is None:  # 如果当前没有embedding,按照传入vec的shape进行构造随机矩阵
                        n_dims = len(vec)
                        self.embeddings = np.random.normal(np.zeros((vocab_size, n_dims))).astype(dtype)
                        self.embeddings[self.PAD] = np.zeros(n_dims)
                    self.embeddings[idx] = vec
                    load_num += 1

        return load_num

    def __getitem__(self, item):
        if type(item) is int:
            return self.index2word[item]
        return self.word2index.get(item, self.UNK)

    def __len__(self):
        return len(self.index2word)

    @lru_cache(maxsize=None)
    def is_word(self, token_id: int) -> bool:
        """Return whether the token at `token_id` is a word; False for punctuations."""
        if token_id < 4: return False
        if token_id >= len(self): return True  # OOV is assumed to be words
        token_str = self.index2word[token_id]
        if not word_detector.search(token_str) or token_str == '<P>':
            return False
        return True


class Example(NamedTuple):
    src: List[str]
    tgt: List[str]
    src_len: int  # inclusive of EOS, so that it corresponds to tensor shape
    tgt_len: int  # inclusive of EOS, so that it corresponds to tensor shape


class OOVDict(object):

    def __init__(self, base_oov_idx):
        self.word2index = {}  # type: Dict[Tuple[int, str], int]
        self.index2word = {}  # type: Dict[Tuple[int, int], str]
        self.next_index = {}  # type: Dict[int, int]
        self.base_oov_idx = base_oov_idx
        self.ext_vocab_size = base_oov_idx

    def add_word(self, idx_in_batch, word) -> int:
        key = (idx_in_batch, word)
        index = self.word2index.get(key)
        if index is not None: return index
        index = self.next_index.get(idx_in_batch, self.base_oov_idx)
        self.next_index[idx_in_batch] = index + 1
        self.word2index[key] = index
        self.index2word[(idx_in_batch, index)] = word
        self.ext_vocab_size = max(self.ext_vocab_size, index + 1)
        return index


class Batch(NamedTuple):
    examples: List[Example]
    input_tensor: Optional[torch.Tensor]
    target_tensor: Optional[torch.Tensor]
    input_lengths: Optional[List[int]]
    oov_dict: Optional[OOVDict]

    @property
    def ext_vocab_size(self):
        if self.oov_dict is not None:
            return self.oov_dict.ext_vocab_size
        return None


def simple_tokenizer(text: str, lower: bool = False, newline: str = None) -> List[str]:
    """Split an already tokenized input `text`."""
    if lower:
        text = text.lower()
    if newline is not None:  # replace newline by a token
        text = text.replace('\n', ' ' + newline + ' ')
    return text.split()


class Dataset(object):

    def __init__(self, filename: str, tokenize: Callable = simple_tokenizer, max_src_len: int = None,
                 max_tgt_len: int = None, truncate_src: bool = False, truncate_tgt: bool = False):
        print("Reading dataset %s..." % filename, end=' ', flush=True)
        self.filename = filename
        self.pairs = []
        self.src_len = 0
        self.tgt_len = 0
        with open(filename, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f.readlines()):
                tmp = json.loads(line)
                cols = list(tmp.keys())  # 获取json的列名
                col1, col2 = cols[0], cols[1]  # 按照字典序排序
                src = tokenize(tmp[col1])
                if max_src_len and len(src) > max_src_len:
                    if truncate_src:
                        src = src[:max_src_len]
                    else:
                        continue
                tgt = tokenize(tmp[col2])
                if max_tgt_len and len(tgt) > max_tgt_len:
                    if truncate_tgt:
                        tgt = tgt[:max_tgt_len]
                    else:
                        continue
                src_len = len(src) + 1  # EOS
                tgt_len = len(tgt) + 1  # EOS
                self.src_len = max(self.src_len, src_len)
                self.tgt_len = max(self.tgt_len, tgt_len)
                self.pairs.append(Example(src, tgt, src_len, tgt_len))
        print('%d pairs.' % len(self.pairs))

    def build_vocab(self, vocab_size: int = None, src: bool = True, tgt: bool = True,
                    embed_file: str = None) -> Vocab:
        filename, _ = os.path.splitext(self.filename)
        if vocab_size:
            filename += "_%d" % vocab_size
        filename += '_vocab'
        if os.path.isfile(filename):
            print(filename)
            vocab = torch.load(filename)
            print("Vocabulary loaded, %d words." % len(vocab))
        else:
            print("Building vocabulary...", end=' ', flush=True)
            vocab = Vocab()
            for example in self.pairs:
                if src:
                    vocab.add_words(example.src)
                if tgt:
                    vocab.add_words(example.tgt)
            vocab.trim(vocab_size=vocab_size)
            print("%d words." % len(vocab))
            torch.save(vocab, filename)
        if embed_file:
            count = vocab.load_embeddings(embed_file)
            print("%d pre-trained embeddings loaded." % count)
        return vocab

    def generator(self, batch_size: int, src_vocab: Vocab = None, tgt_vocab: Vocab = None,
                  ext_vocab: bool = False):
        ptr = len(self.pairs)  # make sure to shuffle at first run
        if ext_vocab:
            assert src_vocab is not None
            base_oov_idx = len(src_vocab)
        while True:
            if ptr + batch_size > len(self.pairs):
                shuffle(self.pairs)  # shuffle inplace to save memory
                ptr = 0
            examples = self.pairs[ptr:ptr + batch_size]
            ptr += batch_size
            src_tensor, tgt_tensor = None, None
            lengths, oov_dict = None, None
            if src_vocab or tgt_vocab:
                # initialize tensors
                if src_vocab:
                    examples.sort(key=lambda x: -x.src_len)
                    lengths = [x.src_len for x in examples]
                    max_src_len = lengths[0]
                    src_tensor = torch.zeros(max_src_len, batch_size, dtype=torch.long)
                    if ext_vocab:
                        oov_dict = OOVDict(base_oov_idx)
                if tgt_vocab:
                    max_tgt_len = max(x.tgt_len for x in examples)
                    tgt_tensor = torch.zeros(max_tgt_len, batch_size, dtype=torch.long)
                # fill up tensors by word indices
                for i, example in enumerate(examples):
                    if src_vocab:
                        for j, word in enumerate(example.src):
                            idx = src_vocab[word]
                            if ext_vocab and idx == src_vocab.UNK:
                                idx = oov_dict.add_word(i, word)
                            src_tensor[j, i] = idx
                        src_tensor[example.src_len - 1, i] = src_vocab.EOS
                    if tgt_vocab:
                        for j, word in enumerate(example.tgt):
                            idx = tgt_vocab[word]
                            if ext_vocab and idx == src_vocab.UNK:
                                idx = oov_dict.word2index.get((i, word), idx)
                            tgt_tensor[j, i] = idx
                        tgt_tensor[example.tgt_len - 1, i] = tgt_vocab.EOS
            yield Batch(examples, src_tensor, tgt_tensor, lengths, oov_dict)


class Hypothesis(object):

    def __init__(self, tokens, log_probs, dec_hidden, dec_states, enc_attn_weights, num_non_words):
        self.tokens = tokens  # type: List[int]
        self.log_probs = log_probs  # type: List[float]
        self.dec_hidden = dec_hidden  # shape: (1, 1, hidden_size)
        self.dec_states = dec_states  # list of dec_hidden
        self.enc_attn_weights = enc_attn_weights  # list of shape: (1, 1, src_len)
        self.num_non_words = num_non_words  # type: int

    def __repr__(self):
        return repr(self.tokens)

    def __len__(self):
        return len(self.tokens) - self.num_non_words

    @property
    def avg_log_prob(self):
        return sum(self.log_probs) / len(self.log_probs)

    def create_next(self, token, log_prob, dec_hidden, add_dec_states, enc_attn, non_word):
        return Hypothesis(tokens=self.tokens + [token], log_probs=self.log_probs + [log_prob],
                          dec_hidden=dec_hidden, dec_states=
                          self.dec_states + [dec_hidden] if add_dec_states else self.dec_states,
                          enc_attn_weights=self.enc_attn_weights + [enc_attn]
                          if enc_attn is not None else self.enc_attn_weights,
                          num_non_words=self.num_non_words + 1 if non_word else self.num_non_words)


def show_plot(loss, step=1, val_loss=None, val_metric=None, val_step=1, file_prefix=None):
    plt.figure()
    fig, ax = plt.subplots(figsize=(12, 8))
    # this locator puts ticks at regular intervals
    loc = ticker.MultipleLocator(base=0.2)
    ax.yaxis.set_major_locator(loc)
    ax.set_ylabel('Loss', color='b')
    ax.set_xlabel('Batch')
    plt.plot(range(step, len(loss) * step + 1, step), loss, 'b')
    if val_loss:
        plt.plot(range(val_step, len(val_loss) * val_step + 1, val_step), val_loss, 'g')
    if val_metric:
        ax2 = ax.twinx()
        ax2.plot(range(val_step, len(val_metric) * val_step + 1, val_step), val_metric, 'r')
        ax2.set_ylabel('ROUGE', color='r')
    if file_prefix:
        plt.savefig(file_prefix + '.png')
        plt.close()


def show_attention_map(src_words, pred_words, attention, pointer_ratio=None):
    fig, ax = plt.subplots(figsize=(16, 4))
    im = plt.pcolormesh(np.flipud(attention), cmap="GnBu")
    # set ticks and labels
    ax.set_xticks(np.arange(len(src_words)) + 0.5)
    ax.set_xticklabels(src_words, fontsize=14)
    ax.set_yticks(np.arange(len(pred_words)) + 0.5)
    ax.set_yticklabels(reversed(pred_words), fontsize=14)
    if pointer_ratio is not None:
        ax1 = ax.twinx()
        ax1.set_yticks(np.concatenate([np.arange(0.5, len(pred_words)), [len(pred_words)]]))
        ax1.set_yticklabels('%.3f' % v for v in np.flipud(pointer_ratio))
        ax1.set_ylabel('Copy probability', rotation=-90, va="bottom")
    # let the horizontal axes labelling appear on top
    ax.tick_params(top=True, bottom=False, labeltop=True, labelbottom=False)
    # rotate the tick labels and set their alignment
    plt.setp(ax.get_xticklabels(), rotation=-45, ha="right", rotation_mode="anchor")


def format_tokens(tokens: List[str], newline: str = '<P>', for_rouge: bool = False) -> str:
    """Join output `tokens` for ROUGE evaluation."""
    tokens = filter(lambda t: t not in not_for_output, tokens)
    if for_rouge:
        tokens = [non_word_char_in_word.sub("", t) for t in tokens]  # "n't" => "nt"
    if newline is None:
        s = ' '.join(tokens)
    else:  # replace newline tokens by newlines
        lines, line = [], []
        for tok in tokens:
            if tok == newline:
                if line: lines.append(" ".join(line))
                line = []
            else:
                line.append(tok)
        if line: lines.append(" ".join(line))
        s = '\n'.join(lines)
    return s


def format_rouge_scores(rouge_result: Dict[str, float]) -> str:
    lines = []
    line, prev_metric = [], None
    for key in sorted(rouge_result.keys()):
        metric = key.rsplit("_", maxsplit=1)[0]
        if metric != prev_metric and prev_metric is not None:
            lines.append("\t".join(line))
            line = []
        line.append("%s %s" % (key, rouge_result[key]))
        prev_metric = metric
    lines.append("\t".join(line))
    return "\n".join(lines)


def rouge(target: List[List[str]], *predictions: List[List[str]]) -> List[Dict[str, float]]:
    """

    :param target: decode batch: 一个batch的ground truth
    :param predictions: 一个或两个系统生成的batch
    :return:
    """
    results = [dict() for _ in range(len(predictions))]

    for sys_id, preds in enumerate(predictions):
        # preds 为 batchsize个List[words]
        rouge_l = []
        for hyp, ref in zip(preds, target):
            hyp = ' '.join(hyp)
            ref = ' '.join(ref)
            scores = evaluator.get_scores(hyp, ref)
            rouge_l.append(scores['rouge-l']['f'])

        results[sys_id]['l_f'] = np.mean(rouge_l)

    return results


def rouge_single(example: List[List[str]]) -> List[Dict[str, float]]:
    """Helper for `rouge_parallel()`."""
    return rouge(*example)


def rouge_parallel(target: List[List[str]], *predictions: List[List[str]]) \
        -> List[List[Dict[str, float]]]:
    """
    Run ROUGE tests in parallel (by Python multi-threading, i.e. multiprocessing.dummy) to obtain
    per-document scores. Depending on batch size and hardware, this may be slower or faster than
    `rouge()`.
    """
    with Pool() as p:
        return p.map(rouge_single, zip(target, *predictions))