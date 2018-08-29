import math
from os.path import expanduser, join

import numpy as np

import torch
from torch import nn
from torch import optim
from torchtext import data
from torch.nn import functional as F
from torch.optim.lr_scheduler import _LRScheduler


PATH = expanduser(join('~', 'data', 'fastai', 'nietzsche'))
TRAIN_PATH = join(PATH, 'trn')
VALID_PATH = join(PATH, 'val')


DEVICE = 'cuda:0' if torch.cuda.is_available() else 'cpu'


def prepare_dataset(filename):
    text = []
    field = data.Field(lower=True, tokenize=list)
    with open(filename) as file:
        for line in file:
            text += field.preprocess(line)
    text += '<eos>'
    field.build_vocab(text, min_freq=3)
    indexes = field.numericalize(text)
    return field, indexes.view(-1)


class SequenceIterator:

    def __init__(self, seq, bptt=10, batch_size=64, random_length=True):
        # Converting dataset into batches:
        # 1) truncate text length to evenly fit into number of batches
        # 2) reshape the text into N (# of batches) * M (batch size)
        # 3) transpose to convert into "long" format with fixed number of cols

        n_batches = seq.size(0) // batch_size
        truncated = seq[:n_batches * batch_size]
        batches = truncated.view(batch_size, -1).t().contiguous()

        self.bptt = bptt
        self.batch_size = batch_size
        self.random_length = random_length
        self.batches = batches
        self.curr_line = 0
        self.curr_iter = 0
        self.total_lines = batches.size(0)
        self.total_iters = self.total_lines // self.bptt - 1

    @property
    def completed(self):
        if self.curr_line >= self.total_lines - 1:
            return True
        if self.curr_iter >= self.total_iters:
            return True
        return False

    def __iter__(self):
        self.curr_line = self.curr_iter = 0
        return self

    def __next__(self):
        return self.next()

    def next(self):
        if self.completed:
            raise StopIteration()
        seq_len = self.get_sequence_length()
        batch = self.get_batch(seq_len)
        self.curr_line += seq_len
        self.curr_iter += 1
        return batch

    def get_sequence_length(self):
        seq_len = self.bptt
        if self.random_length is not None:
            bptt = self.bptt
            if np.random.random() >= 0.95:
                bptt /= 2
            seq_len = max(5, int(np.random.normal(bptt, 5)))
        return seq_len

    def get_batch(self, seq_len):
        i, source = self.curr_line, self.batches
        seq_len = min(seq_len, self.total_lines - 1 - i)
        X = source[i:i + seq_len].contiguous()
        y = source[(i + 1):(i + 1) + seq_len].contiguous()
        return X, y


class CosineAnnealingLR(_LRScheduler):

    def __init__(self, optimizer, t_max=200, eta_min=0.0005,
                 cycle_mult=2, last_epoch=-1):

        self.t_max = t_max
        self.eta_min = eta_min
        self.cycle_mult = cycle_mult
        self.cycle_epoch = last_epoch
        super().__init__(optimizer, last_epoch)

    def get_lr(self):
        self.cycle_epoch += 1

        t_max = self.t_max
        eta_min = self.eta_min
        t = self.cycle_epoch % t_max

        learning_rates = []
        for lr in self.base_lrs:
            delta = lr - eta_min
            new_lr = eta_min + delta*(1 + math.cos(math.pi * t/t_max)) / 2
            learning_rates.append(new_lr)

        if t == 0:
            self.cycle_epoch = 0
            self.t_max *= self.cycle_mult

        return learning_rates


class RNN(nn.Module):

    def __init__(self, vocab_size, n_factors, batch_size, n_hidden,
                 architecture='rnn', device=DEVICE):

        num_of_states = 1

        if architecture == 'rnn':
            rnn = nn.RNN
        elif architecture == 'gru':
            rnn = nn.GRU
        elif architecture == 'lstm':
            rnn = nn.LSTM
            num_of_states += 1
        else:
            raise ValueError(f'unexpected network type: {architecture}')

        self.vocab_size = vocab_size
        self.n_hidden = n_hidden
        self.num_of_states = num_of_states
        self.device = device

        super().__init__()
        self.embed = nn.Embedding(vocab_size, n_factors)
        self.rnn = rnn(n_factors, n_hidden)
        self.out = nn.Linear(n_hidden, vocab_size)
        self.hidden_state = self.init_hidden(batch_size).to(device)
        self.to(device)

    def forward(self, batch):
        embeddings = self.embed(batch)
        rnn_outputs, h = self.rnn(embeddings, self.hidden_state)
        self.hidden_state = truncate_history(h)
        linear = self.out(rnn_outputs)
        return F.log_softmax(linear, dim=-1).view(-1, self.vocab_size)

    def init_hidden(self, batch_size):
        return torch.zeros(self.num_of_states, 1, batch_size, self.n_hidden)


def truncate_history(v):
    if type(v) == torch.Tensor:
        return v.detach()
    else:
        return tuple(truncate_history(x) for x in v)


class StringBuilder:
    """
    The helper class used during debugging process to convert tensors with
    integer indexes into strings with batches of text they represent.
    """
    def __init__(self, field):
        self.field = field

    def __call__(self, tensor):
        return '\n'.join([
            ''.join([
                self.field.vocab.itos[char]
                for char in line])
            for line in tensor])


def main():
    bs = 64
    bptt = 8
    n_factors = 42
    n_hidden = 256

    field, indexes = prepare_dataset(join(TRAIN_PATH, 'train.txt'))
    iterator = SequenceIterator(indexes, bptt, bs)
    vocab_size = len(field.vocab.itos)

    model = RNN(vocab_size, n_factors, bs, n_hidden, architecture='lstm')
    optimizer = optim.RMSprop(model.parameters(), lr=1e-3)
    sched = CosineAnnealingLR(optimizer, t_max=iterator.total_iters)

    alpha = 0.98
    avg_loss = 0.0
    batch_num = 0

    for epoch in range(1, 11):
        epoch_loss = 0
        for x, y in iterator:
            batch_num += 1
            sched.step()
            model.zero_grad()
            output = model(x)
            loss = F.nll_loss(output, y.view(-1))
            loss.backward()
            optimizer.step()
            avg_loss = avg_loss*alpha + loss.item()*(1 - alpha)
            epoch_loss = avg_loss/(1 - alpha**batch_num)
        print('Epoch %03d loss: %2.4f' % (epoch, epoch_loss))


if __name__ == '__main__':
    main()