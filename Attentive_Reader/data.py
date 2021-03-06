import logging
import random
import numpy

import cPickle

from picklable_itertools import iter_

from fuel.datasets import Dataset
from fuel.streams import DataStream
from fuel.schemes import IterationScheme, ConstantScheme
from fuel.transformers import Batch, Mapping, SortMapping, Unpack, Padding, Transformer

import sys
import os

logging.basicConfig(level='INFO')
logger = logging.getLogger(__name__)


# get_data explains what to return
class QADataset(Dataset):
    def __init__(self, path, vocab_file, n_entities, need_sep_token, **kwargs):
        self.provides_sources = ('context', 'question', 'answer', 'candidates')
        # , 'context_actual', 'question_actual', 'answer_actual'

        self.path = path

        self.vocab = ['@entity%d' % i for i in range(n_entities)] + \
                     [w.rstrip('\n') for w in open(vocab_file)] + \
                     ['<UNK>', '@placeholder'] + \
                     (['<SEP>'] if need_sep_token else [])

        self.n_entities = n_entities
        self.vocab_size = len(self.vocab)
        self.reverse_vocab = {w: i for i, w in enumerate(self.vocab)}
        # creating the opposite mapping
        self.id_to_vocab = {i: w for i, w in enumerate(self.vocab)}

        super(QADataset, self).__init__(**kwargs)

    def to_word_id(self, w, cand_mapping):
        if w in cand_mapping:
            return cand_mapping[w]
        elif w[:7] == '@entity':
            # print('iterating through cand mapping')
            # for (k, v) in  cand_mapping.iteritems():
            #     print(k)
            #     print(v)
            raise ValueError("Unmapped entity token: %s" % w)
        elif w in self.reverse_vocab:
            return self.reverse_vocab[w]
        else:
            return self.reverse_vocab['<UNK>']

    def to_word_ids(self, s, cand_mapping):
        return numpy.array([self.to_word_id(x, cand_mapping) for x in s.split(' ')], dtype=numpy.int32)

    def get_data(self, state=None, request=None):
        if request is None or state is not None:
            raise ValueError("Expected a request (name of a question file) and no state.")

        lines = [l.rstrip('\n') for l in open(os.path.join(self.path, request))]

        ctx = lines[2]
        # print(ctx)
        q = lines[4]
        # print(q)
        a = lines[6]
        # print(a)
        cand = [s.split(':')[0] for s in lines[8:]]
        # print(cand)

        entities = range(self.n_entities)
        while len(cand) > len(entities):
            logger.warning("Too many entities (%d) for question: %s, using duplicate entity identifiers"
                           % (len(cand), request))
            entities = entities + entities
        random.shuffle(entities)
        cand_mapping = {t: k for t, k in zip(cand, entities)}
        # print('iterating through cand mapping')
        # for (k, v) in  cand_mapping.iteritems():
        #     print(k)
        #     print(v)

        ctx = self.to_word_ids(ctx, cand_mapping)
        q = self.to_word_ids(q, cand_mapping)

        cand = numpy.array([self.to_word_id(x, cand_mapping) for x in cand], dtype=numpy.int32)
        a = numpy.int32(self.to_word_id(a, cand_mapping))

        if not a < self.n_entities:
            raise ValueError("Invalid answer token %d" % a)
        if not numpy.all(cand < self.n_entities):
            raise ValueError("Invalid candidate in list %s" % repr(cand))
        if not numpy.all(ctx < self.vocab_size):
            raise ValueError("Context word id out of bounds: %d" % int(ctx.max()))
        if not numpy.all(ctx >= 0):
            raise ValueError("Context word id negative: %d" % int(ctx.min()))
        if not numpy.all(q < self.vocab_size):
            raise ValueError("Question word id out of bounds: %d" % int(q.max()))
        if not numpy.all(q >= 0):
            raise ValueError("Question word id negative: %d" % int(q.min()))

        # print('type2')
        # print type(ctx)
        return ctx, q, a, cand
        # , lines[4], lines[4], lines[6]


# Iterating through the questions
class QAIterator(IterationScheme):
    requests_examples = True

    def __init__(self, path, shuffle=False, **kwargs):
        self.path = path
        self.shuffle = shuffle

        super(QAIterator, self).__init__(**kwargs)

    def get_request_iterator(self):
        l = [f for f in os.listdir(self.path)
             if os.path.isfile(os.path.join(self.path, f))]
        if self.shuffle:
            random.shuffle(l)
        return iter_(l)


# -------------- DATASTREAM SETUP --------------------


class ConcatCtxAndQuestion(Transformer):
    produces_examples = True

    def __init__(self, stream, concat_question_before, separator_token=None, **kwargs):
        assert stream.sources == ('context', 'question', 'answer', 'candidates', 'question_actual')
        self.sources = ('question', 'answer', 'candidates', 'question_actual')

        self.sep = numpy.array([separator_token] if separator_token is not None else [],
                               dtype=numpy.int32)
        self.concat_question_before = concat_question_before

        super(ConcatCtxAndQuestion, self).__init__(stream, **kwargs)

    def get_data(self, request=None):
        if request is not None:
            raise ValueError('Unsupported: request')

        ctx, q, a, cand = next(self.child_epoch_iterator)

        if self.concat_question_before:
            return numpy.concatenate([q, self.sep, ctx]), a, cand
        else:
            return numpy.concatenate([ctx, self.sep, q]), a, cand


class _balanced_batch_helper(object):
    def __init__(self, key):
        self.key = key

    def __call__(self, data):
        return data[self.key].shape[0]


def setup_datastream(path, vocab_file, config):
    ds = QADataset(path, vocab_file, config.n_entities, need_sep_token=config.concat_ctx_and_question)
    it = QAIterator(path, shuffle=config.shuffle_questions)

    stream = DataStream(ds, iteration_scheme=it)

    if config.concat_ctx_and_question:
        stream = ConcatCtxAndQuestion(stream, config.concat_question_before, ds.reverse_vocab['<SEP>'])

    # Sort sets of multiple batches to make batches of similar sizes
    stream = Batch(stream, iteration_scheme=ConstantScheme(config.batch_size * config.sort_batch_count))
    comparison = _balanced_batch_helper(
        stream.sources.index('question' if config.concat_ctx_and_question else 'context'))
    stream = Mapping(stream, SortMapping(comparison))
    stream = Unpack(stream)

    print('sources')
    print(stream.sources)

    stream = Batch(stream, iteration_scheme=ConstantScheme(config.batch_size))
    stream = Padding(stream, mask_sources=['context', 'question', 'candidates'], mask_dtype='int32')

    print('sources2')
    print(stream.sources)

    return ds, stream


if __name__ == "__main__":
    # Test
    class DummyConfig:
        def __init__(self):
            self.shuffle_entities = True
            self.shuffle_questions = False
            self.concat_ctx_and_question = False
            self.concat_question_before = False
            self.batch_size = 2
            self.sort_batch_count = 1000


    ds, stream = setup_datastream(os.path.join(os.getenv("DATAPATH"), "deepmind-qa/cnn/questions/training"),
                                  os.path.join(os.getenv("DATAPATH"), "deepmind-qa/cnn/stats/training/vocab.txt"),
                                  DummyConfig())
    it = stream.get_epoch_iterator()

    for i, d in enumerate(stream.get_epoch_iterator()):
        print '--'
        print d
        if i > 2: break

# vim: set sts=4 ts=4 sw=4 tw=0 et :
