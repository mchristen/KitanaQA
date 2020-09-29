import pickle
import hashlib
import sys
from torch.utils.data import Dataset
import torch
import copy
from torch.utils.data import DataLoader
import math
from collections import Counter
from doggmentator.term_replacement import *
import logging
logger = logging.getLogger().setLevel('INFO')
from datetime import datetime

data_file = pkg_resources.resource_filename(
            'doggmentator', 'support/squad_bert_importance_scores.pkl')
with open(data_file, 'rb') as f:
    score_dict = pickle.load(f)

def _from_checkpoint(
        fname: str='checkpoint.pkl') -> Dict:
    """ Load a checkpoint file """
    with open(fname, 'rb') as f:
        checkpoint = pickle.load(f)
    return checkpoint

def format_squad(
        examples: Dict,
        title_map: Dict,
        context_map: Dict,
        version: str='1.1') -> Dict:
    """ Convert a flat list of dicts to nested SQuAD format """
    squad_data = {}
    squad_data['version'] = version
    squad_data['data'] = []
    qas = []
    current_tle_id = None
    ctx_ids = []
    tle_ids = []
    graphs = []
    num_examples = len(examples)
    unique_ids = [str(x) for x in range(num_examples)]

    dataset = {}
    for example in examples:
        qid = example['id']
        ctx_id = example['ctx_id']
        tle_id = example['tle_id']
        if tle_id not in dataset:
            dataset[tle_id] = {}
        if ctx_id not in dataset[tle_id]:
            dataset[tle_id][ctx_id] = []
        if not all([x['text'] for x in example['answers']]):
            raise Exception('No answer found')
        if not all([x['answer_start'] is not None for x in example['answers']]):
            raise Exception('No answer_start found')
        if not example['question']:
            print('No question: ', example['aug_type'])
            continue
        dataset[tle_id][ctx_id].append({
            'answers':example['answers'],
            'question':example['question'],
            'orig_id':qid,
            'title_id':tle_id,
            'context_id':ctx_id,
            'id':qid+unique_ids.pop(),
            'aug_type':example['aug_type']
        })

    formatted = {'version':version,'data':[]}
    for k,v in dataset.items():
        graphs = []
        for j,q in v.items(): 
            graphs.append({
                'context':context_map[j],
                'qas':q
            })
        formatted['data'].append({
            'title':title_map[k],
            'paragraphs':graphs
        })
    return formatted
            

class MySQuADDataset(Dataset):
    def __init__(
                self,
                raw_examples: List,
                importance_score_dict: List[tuple]=None,
                is_training: bool=False,
                sample_ratio: float=4.,
                num_replacements: int=2,
                sampling_k: int=3,
                sampling_strategy: str='topK',
                p_replace: float=0.1,
                p_dropword: float=0.1,
                p_misspelling: float=0.1,
                save_freq: int=100,
                from_checkpoint: bool=False,
                out_prefix: str='dev'):
        """ Instantiate a MySQuADDataset instance"""
        hparams = {
            "num_replacements":num_replacements,
            "sample_ratio":sample_ratio,
            "p_replace":p_replace,
            "p_dropword":p_dropword,
            "p_misspelling":p_misspelling,
            "sampling_strategy":sampling_strategy,
            "sampling_k":sampling_k
        }
        print('Running MySQuADDataset with hparams {}'.format(hparams))
        # A list of tuples or tensors
        aug_dataset = []
        aug_seqs = []
        examples = []
        title_map = {}
        context_map = {}
        ctx_id = 0
        new_squad_examples = copy.deepcopy(raw_examples)
        for j,psg in enumerate(raw_examples['data']):
            title = psg['title']
            tle_id = j
            title_map[tle_id] = title
            new_squad_examples['data'][j]['title_id'] = str(tle_id)
            for n,para in enumerate(psg['paragraphs']):
                context = para['context']
                context_map[ctx_id] = context
                new_squad_examples['data'][j]['paragraphs'][n]['context_id'] = str(ctx_id)
                for qa in para['qas']:
                    examples.append({
                        'qid':qa['id'],
                        'ctx_id':ctx_id,
                        'tle_id':tle_id,
                        'answers':qa['answers'],
                        'question':qa['question']
                    })
                ctx_id += 1
        with open('train-squadv1.json', 'w') as f:
            json.dump(new_squad_examples, f)

        aug_examples = copy.deepcopy(examples)

        # Normalize probabilities of each augmentation
        probs = [p_dropword, p_replace, p_misspelling]
        probs = [p / sum(probs) for p in probs]
        augmentation_types = {
            'drop': DropTerms(),
            'synonym': ReplaceTerms(rep_type='synonym'),
            'misspelling': ReplaceTerms(rep_type='misspelling')
        }
        num_examples = len(examples)
        num_aug_examples = math.ceil(num_examples * sample_ratio)
        print('Generating {} aug examples from {} orig examples'.format(num_aug_examples, num_examples))
        orig_indices = list(range(num_examples))

        # Randomly sample indices of data in original dataset with replacement
        aug_indices = np.random.choice(orig_indices, size=num_aug_examples)
        aug_freqs = Counter(aug_indices)
        #print(examples, num_examples, num_aug_examples, orig_indices, aug_indices, aug_indices)

        ct = 0
        if from_checkpoint:
            checkpoint = _from_checkpoint()
            if not checkpoint:
                raise RuntimeError('Failed to load checkpoint file')
            aug_freqs = checkpoint['aug_freqs']
            aug_dataset = checkpoint['aug_dataset']
            hparams = checkpoint['hparams']
            ct = checkpoint['ct']

        # Reamining number of each agumentation types after exhausting previous example's variations
        remaining_count = {}
        for aug_type in augmentation_types.keys():
            remaining_count[aug_type] = 0

        for aug_idx, count in aug_freqs.items():
            if len(aug_dataset) > num_aug_examples:
                continue
            # Get frequency of each augmentation type for current example with replacement
            aug_type_sample = np.random.choice(list(augmentation_types.keys()), size=count, p=probs)
            aug_type_freq = Counter(aug_type_sample)
            for aug_type in aug_type_freq.keys():
                aug_type_freq[aug_type] += remaining_count[aug_type]

            # Get raw data from original dataset and get corresponding importance score
            raw_data = examples[aug_idx]
            question = raw_data['question']
            answers = raw_data['answers']
            qid = raw_data['qid']
            ctx_id = raw_data['ctx_id']
            tle_id = raw_data['tle_id']
            if importance_score_dict and qid in importance_score_dict:
                importance_score = importance_score_dict[qid]
            else:
                importance_score = None

            if ct % save_freq == 0:
                print('ct: ', ct)
                print('Generated {} examples'.format(len(aug_dataset)))
                checkpoint = {
                    'aug_freqs':aug_freqs,
                    'aug_dataset':aug_dataset,
                    'hparams':hparams,
                    'ct':ct
                }
                with open('checkpoint.pkl', 'wb') as f:
                    pickle.dump(checkpoint, f) 
            sys.stdout.flush()

            for aug_type, aug_times in aug_type_freq.items():
                # Randomly select a number of terms to replace
                # up to the max `num_replacements`
                reps = np.random.choice(np.arange(num_replacements), 1, replace=False)[0]

                if aug_type == 'drop':
                    # Generate a dropword perturbation
                    aug_questions = augmentation_types[aug_type].drop_terms(
                                                            question,
                                                            num_terms=reps,
                                                            num_output_sents=aug_times)
                else:
                    # Generate synonym and misspelling perturbations
                    aug_questions = augmentation_types[aug_type].replace_terms(
                                                            sentence = question,
                                                            importance_scores = importance_score,
                                                            num_replacements = reps,
                                                            num_output_sents = aug_times,
                                                            sampling_strategy = sampling_strategy,
                                                            sampling_k = sampling_k)
                    # Add an additional drop perturbation to each generated question
                    aug_questions += [
                                        augmentation_types['drop'].drop_terms(
                                                        x,
                                                        num_terms=reps,
                                                        num_output_sents=1)
                                        for x in aug_questions
                                    ]
                                                        
                for aug_question in aug_questions:
                    if is_training:
                        aug_dataset.append({
                                                'id':qid,
                                                'ctx_id':ctx_id,
                                                'tle_id':tle_id,
                                                'aug_type':aug_type,
                                                'question':aug_question,
                                                'answers':answers,
                                                'is_impossible':is_impossible
                                        })
                    else:
                        aug_seqs.append({'orig': question, 'aug': aug_question, 'type':aug_type})
                        aug_dataset.append({
                                                'id':qid,
                                                'ctx_id':ctx_id,
                                                'tle_id':tle_id,
                                                'aug_type':aug_type,
                                                'question':aug_question,
                                                'answers':answers,
                                        })  

                remaining_count[aug_type] = aug_times - len(aug_questions)

            ct += 1

        formatted_aug_dataset = format_squad(aug_dataset, title_map, context_map)
        print('Saving data')
        with open(out_prefix+'_aug_seqs.json', 'w') as f:
            json.dump(aug_seqs, f)
        with open(out_prefix+'_aug_squad_v1.json', 'w') as f:
            json.dump(formatted_aug_dataset, f)
        with open('hparams.json', 'w') as f:
            json.dump(hparams, f)


class MyTensorDataset(Dataset):
    def __init__(self, raw_dataset, tokenizer, importance_score_dict, is_training=False, sample_ratio=0.5,
                 p_replace=0.1, p_dropword=0.1, p_misspelling=0.1):
        # A list of tuples of tensors
        self.dataset = []
        aug_dataset = []
        aug_seqs = []
        raw_dataset = [raw_dataset.__getitem__(idx) for idx in range(len(raw_dataset))]

        # Normalize probabilities of each augmentation
        probs = [p_dropword, p_replace, p_misspelling]
        probs = [p / sum(probs) for p in probs]
        augmentation_types = {
            'drop': dropwords,
            'synonym': ReplaceTerms(rep_type='synonym'),
            'misspelling': ReplaceTerms(rep_type='misspelling')
        }
        num_examples = len(raw_dataset)
        num_aug_examples = math.ceil(num_examples * sample_ratio)
        orig_indices = list(range(num_examples))

        # Randomly sample indices of data in original dataset with replacement
        aug_indices = np.random.choice(orig_indices, size=num_aug_examples)
        aug_freqs = Counter(aug_indices)

        # Reamining number of each agumentation types after exhausting previous example's variations
        remaining_count = {}
        for aug_type in augmentation_types.keys():
            remaining_count[aug_type] = 0

        ct = 0

        for aug_idx, count in aug_freqs.items():
            # Get frequency of each augmentation type for current example with replacement
            aug_type_sample = np.random.choice(list(augmentation_types.keys()), size=count, p=probs)
            aug_type_freq = Counter(aug_type_sample)
            for aug_type in aug_type_freq.keys():
                aug_type_freq[aug_type] += remaining_count[aug_type]

            # Get raw data from original dataset and get corresponding importance score
            raw_data = raw_dataset[aug_idx]
            if is_training:
                input_ids, attention_masks, token_type_ids, start_positions, end_positions, cls_index, p_mask, is_impossible = raw_data
            else:
                input_ids, attention_masks, token_type_ids, feature_index, cls_index, p_mask = raw_data
            seq_len = len(input_ids)  # sequence length for padding
            tokens = tokenizer.convert_ids_to_tokens(input_ids)
            sep_id = [idx for idx, token in enumerate(tokens) if token == '[SEP]']
            question = tokenizer.convert_tokens_to_string(tokens[1:sep_id[0]])
            context = tokenizer.convert_tokens_to_string(tokens[sep_id[0] + 1:sep_id[1]])
            importance_score = importance_score_dict[aug_idx]

            if ct % 100 == 0:
                print('ct: ', ct)
                print('Generated {} examples'.format(len(aug_dataset)))
            
            for aug_type, aug_times in aug_type_freq.items():
                if aug_type == 'drop':
                    aug_questions = augmentation_types[aug_type](question, N = 1, K = 1)
                else:
                    aug_questions = augmentation_types[aug_type].replace_terms(sentence = question,
                                                             importance_scores = importance_score,
                                                             num_replacements = 1,
                                                             num_output_sents = aug_times,
                                                             sampling_strategy = 'topK',
                                                             sampling_k = sampling_k)
                for aug_question in aug_questions:
                    data_dict = tokenizer.encode_plus(aug_question, context,
                                                      pad_to_max_length=True,
                                                      max_length=seq_len,
                                                      is_pretokenized=False,
                                                      return_token_type_ids=True,
                                                      return_attention_mask=True,
                                                      return_tensors='pt',
                                                      )
                    if is_training:
                        aug_dataset.append(tuple([data_dict['input_ids'][0],
                                                  data_dict['attention_mask'][0],
                                                  data_dict['token_type_ids'][0],
                                                  start_position,
                                                  end_position,
                                                  cls_index,
                                                  p_mask,
                                                  is_impossible,
                                                  aug_type,
                                                  ]))
                    else:
                        aug_seqs.append({'orig': question, 'aug': aug_question, 'type':aug_type})
                        aug_dataset.append(tuple([data_dict['input_ids'][0],
                                                  data_dict['attention_mask'][0],
                                                  data_dict['token_type_ids'][0],
                                                  feature_index,
                                                  cls_index,
                                                  p_mask,
                                                  aug_type,
                                                  ]))
                if ct % 1000 == 0:
                    print('Generated {} examples'.format(len(aug_dataset)))
                remaining_count[aug_type] = aug_times - len(aug_questions)

            ct += 1

        print('Saving data')
        with open('aug_seqs.json', 'w') as f:
            json.dump(aug_seqs, f)
        torch.save(aug_dataset, "aug_SQuAD_v1_dev.pt")

        self.dataset = raw_dataset + aug_dataset

    def __getitem__(self, index):
        return self.dataset[index]

    def __len__(self):
        return len(self.dataset)



if __name__ == "__main__":
    # Load SQuAD Dataset
    from transformers.data.processors.squad import SquadResult, SquadV1Processor, SquadV2Processor, squad_convert_examples_to_features
    '''
    import tensorflow_datasets as tfds
    tfds_examples = tfds.load("squad")
    examples = SquadV1Processor().get_examples_from_dataset(tfds_examples, evaluate=True)
    from transformers import BertTokenizer, BertModel
    tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
    features, raw_dataset = squad_convert_examples_to_features(
            examples=examples,
            tokenizer=tokenizer,
            max_seq_length=512,
            doc_stride=128,
            max_query_length=64,
            is_training=False,
            return_dataset="pt",
            #threads=args.threads,
    )
    '''

    out_prefix = 'train'
    #dataset = MyTensorDataset(raw_dataset, tokenizer, score_dict)
    fname = '/home/ubuntu/dev/bootcamp/finetune/SQuAD/train/support/'+out_prefix+'-v1.1.json'
    with open(fname, 'r') as f:
        data = json.load(f)
    #dataset = MyQADataset(data, score_dict)
    dataset = MySQuADDataset(data, out_prefix=out_prefix, from_checkpoint=True)
