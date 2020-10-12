import random
import torch
import os
import logging
import requests
import glob
import numpy as np
from typing import Tuple
from torch.utils.data import Dataset
from transformers.data.processors.squad import SquadV1Processor, SquadV2Processor
from transformers import squad_convert_examples_to_features
from prefect import Flow, task
from prefect.utilities.notifications import slack_notifier
from doggmentator.trainer.train import Trainer
from doggmentator.trainer.alum_squad_processor import (
    alum_squad_convert_examples_to_features,
    AlumSquadV1Processor,
    AlumSquadV2Processor
)

from transformers import (
    WEIGHTS_NAME,
    AlbertConfig,
    AlbertForQuestionAnswering,
    AlbertTokenizer,
    BertConfig,
    BertForQuestionAnswering,
    BertTokenizer,
)

MODEL_CLASSES = {
    "albert": (AlbertConfig, AlbertForQuestionAnswering, AlbertTokenizer),
    "bert": (BertConfig, BertForQuestionAnswering, BertTokenizer),
}

logger = logging.getLogger(__name__)


def load_and_cache_examples(
        args,
        tokenizer,
        evaluate=False,
        use_aug_path=False,
        output_examples=False) -> torch.utils.data.TensorDataset:
    
    """Loads SQuAD-like data features from dataset file (or cache)

    Parameters
    ----------
    args : doggmentator.trainer.arguments.ModelArguments
        A set of arguments related to the model. Specifically, the following arguments are used in this function:
        - args.train_file_path : str
              Path to the training data files
        - args.do_aug : bool
              Boolean variable to clarify whether the augmented training is to be used.
        - args.aug_file_path : str
              Path for augmented train dataset
        - args.data_dir : str
              Path for data files
        - args.model_name_or_path : str
              Path to pretrained model or model identifier from huggingface.co/models
        - args.max_seq_length : Optional[int]
              Max length for the input tokens
        - args.overwrite_cache : Bool
              Overwrite cached data on load
        - args.predict_file_path : Dict[str, str]
              Paths for cached eval datasets
        - args.version_2_with_negative : Bool
              SQuAD v2.0 training
        - args.doc_stride : Optional[int]
        - args.max_query_length : Optional[int]
              Max length for the query segment
    tokenizer : 
        The tokenizer used to preprocess the data.
    evaluate : Bool
        A boolean variabla describing whether this is an evaluation or training dataset.
    use_aug_path : Bool
        A boolean variable to clarify whether a augemented data path is given. If it is given, the augmented data path is used loading and caching the data.
    output_examples : Bool
        a boolean variable to clarify whether the examples and the features should also given as an output. If False, the function only returns the dataset.

    Returns
    -------
    torch.utils.data.TensorDataset
        The dataset containing the data to be used for training or evaluation.
        Important Notes:
        - If the output_examples is True, examples and features also are returned.
        - If evaluate = True, the output will be a dictionary for which the keys are the name of the datasets used for evaluation and the values are the dataset (and optionally the examples and features)
    """

    if not args.train_file_path and not (args.do_aug and args.aug_file_path):
        logging.error('load_and_cache_examples requires one of either \"train_file_path\", \"aug_file_path\"')

    # Use the augmented data or the original training data
    train_or_aug_path = args.train_file_path if not use_aug_path else args.aug_file_path

    input_dir = args.data_dir if args.data_dir else "."
    cached_features_file = os.path.join(
        input_dir,
        "cached_{}_{}_{}".format(
            "dev" if evaluate else "train",
            list(filter(None, args.model_name_or_path.split("/"))).pop(),
            str(args.max_seq_length),
        ),
    )

    # Init features and dataset from cache if it exists
    if os.path.exists(cached_features_file) and not args.overwrite_cache:
        logger.info("Loading features from cached file %s", cached_features_file)
        features_and_dataset = torch.load(cached_features_file)
        features, dataset, examples = (
            features_and_dataset["features"],
            features_and_dataset["dataset"],
            features_and_dataset["examples"],
        )
    else:
        logger.info("Creating features from dataset file at %s", input_dir)

        if not args.data_dir and ((evaluate and not args.predict_file_path) or (not evaluate and not train_or_aug_path)):
            try:
                import tensorflow_datasets as tfds
            except ImportError:
                raise ImportError("If not data_dir is specified, tensorflow_datasets needs to be installed.")

            if args.version_2_with_negative:
                logger.warn("tensorflow_datasets does not handle version 2 of SQuAD.")

            tfds_examples = tfds.load("squad")
            examples = SquadV1Processor().get_examples_from_dataset(tfds_examples, evaluate=evaluate)
        else:
            if evaluate:
                # when does it concatenate if eval and train are both true?
                examples = {}
                processor = AlumSquadV2Processor() if args.version_2_with_negative else AlumSquadV1Processor()
                for predict_sets, predict_paths in args.predict_file_path.items():
                    examples[predict_sets] = processor.alum_get_dev_examples(args.data_dir, filename=predict_paths)
                    logger.info("Evaluation Data is fetched for %s.", predict_sets)
            else:
                processor = SquadV2Processor() if args.version_2_with_negative else SquadV1Processor()
                examples = processor.get_train_examples(args.data_dir, filename=train_or_aug_path)
        
        
        if not evaluate:
            features, dataset = squad_convert_examples_to_features(
                examples=examples,
                tokenizer=tokenizer,
                max_seq_length=args.max_seq_length,
                doc_stride=args.doc_stride,
                max_query_length=args.max_query_length,
                is_training=not evaluate,
                return_dataset="pt",
                #threads=args.threads,
            )

            logger.info("Saving features into cached file %s", cached_features_file)
            torch.save({"features": features, "dataset": dataset, "examples": examples}, cached_features_file)
            
        else:
            #TODO: Incremental Cache - The current version will cache all the eval files together.
            features, dataset = {}, {}
            for predict_sets, example in examples.items():
                features[predict_sets], dataset[predict_sets] = alum_squad_convert_examples_to_features(
                    examples=example,
                    tokenizer=tokenizer,
                    max_seq_length=args.max_seq_length,
                    doc_stride=args.doc_stride,
                    max_query_length=args.max_query_length,
                    return_dataset="pt",
                    #threads=args.threads,
                )
                logger.info("Feature Extraction for Evaluation Data from %s is Finished.", predict_sets)
            logger.info("Saving features into cached file %s", cached_features_file)
            torch.save({"features": features, "dataset": dataset, "examples": examples}, cached_features_file)
                
    if output_examples:
        return dataset, examples, features
    return dataset


slack_url = os.environ['SLACK_WEBHOOK_URL'] if 'SLACK_WEBHOOK_URL' in os.environ else None
def post_to_slack(obj, old_state, new_state):
    """
    Post a msg to Slack url if configured, else simply return new_state object
    """
    if slack_url:
        if new_state.is_finished():
            msg = "{0} finished in state {1} --- results {2}".format(obj, new_state, new_state.result)

            # replace URL with your Slack webhook URL
            requests.post(slack_url, json={"text": msg})

    return new_state


@task(name="eval", state_handlers=[post_to_slack])
def eval_task(args):
    """Evaluates the model on a the evaluation datasets

    Parameters
    ----------
    args : tuple
        A tuple including the ModelArguments (doggmentator.trainer.arguments.ModelArguments) and TrainingArguments (transformers.training_args.TrainingArguments). Specifically, the following arguments from the ModelArguments are used in this function:
        - eval_all_checkpoints : bool
              Evaluate all the checkpoints
        - model_name_or_path : str
              Path to pretrained model or model identifier from huggingface.co/models
        - model_type : str
              Currently, one of either 'bert' or 'albert' models
        - tokenizer_name_or_path : str
              Pretrained tokenizer name or path if not the same as model_name
        - cache_dir : str
              The path to store the pretrained models
        The following arguments from the TrainingArguments are used in this function:
        - output_dir : str
              The output directory where the model predictions and checkpoints will be written.

    Returns
    -------
    Dict[str : Dict[str : ModelArguments, str : TrainingArguments, str : OrderedDict]
        The evaluation results for various evaluation datasets and checkpoints. The following example shows the general structure of the evaluation results. 'squad_dev1.1' is the name of the evaluation dataset and '1000' is the checkpoint. To better understand different metrics, please refer to squad_evaluate function in: https://github.com/huggingface/transformers/blob/master/src/transformers/data/metrics/squad_metrics.py 
        Example:
          {'squad_dev1.1': 
            {'1000' : 
              {'model_args' : ModelArguments(...), 
               'training_args': TrainingArguments (...), 
               'eval': OrderedDict(
                                   [('exact', ...), 
                                    ('f1', ...), 
                                    ('total', ...), 
                                    ('HasAns_exact', ...), 
                                    ('HasAns_f1', ...), 
                                    ('HasAns_total', ...), 
                                    ('best_exact', ...), 
                                    ('best_exact_thresh', ...), 
                                    ('best_f1', ...), 
                                    ('best_f1_thresh', ...)
                                   ]
                                  )
             }
           }
         }
    """
    model_args, training_args = args
    all_eval_sets_results = {}
    if model_args.eval_all_checkpoints:
        checkpoints = [training_args.output_dir]
        checkpoints = list(
            os.path.dirname(c)
            for c in sorted(glob.glob(training_args.output_dir + "/**/" + WEIGHTS_NAME, recursive=True))
        )
        checkpoints = [x for x in checkpoints if 'checkpoint' in x]
    else:
        if not os.path.exists(model_args.model_name_or_path):
            raise Exception("Must specify parameter model_name_or_path")
        checkpoints = [model_args.model_name_or_path]
    for checkpoint in checkpoints:
        # Load model and tokenizer
        config, model_cls, tokenizer_cls = MODEL_CLASSES[model_args.model_type]
        tokenizer = tokenizer_cls.from_pretrained(
            model_args.tokenizer_name_or_path if model_args.tokenizer_name_or_path else checkpoint,
            cache_dir=model_args.cache_dir,
        )
        model = model_cls.from_pretrained(
            checkpoint,
            cache_dir=model_args.cache_dir,
        )

        # Initialize the Trainer
        trainer = Trainer(
            data_collator=None,
            model=model,
            tokenizer=tokenizer,
            args=training_args,
            prediction_loss_only=True,
        )

        # Load SQuAD-specific dataset and examples for metric calculation
        dataset, examples, features = load_and_cache_examples(
            model_args,
            tokenizer,
            evaluate=True,
            output_examples=True)
        
        logger.info("Predict Sets are : %s", examples.keys())
        for predict_set in examples:
            results = {}
            model_idx = checkpoint.split("-")[-1]
            print(f'The checkpoint check: {model_idx}')
            if model_args.do_alum:
                results[model_idx] = {
                                    'model_args': model_args,
                                    'training_args':training_args,
                                    'eval':trainer.adv_evaluate(
                                            checkpoint,
                                            model_args,
                                            tokenizer,
                                            dataset[predict_set],
                                            examples[predict_set],
                                            features[predict_set])
                            }
            else:
                results[model_idx] = {
                                    'model_args': model_args,
                                    'training_args':training_args,
                                    'eval':trainer.evaluate(
                                            checkpoint,
                                            model_args,
                                            tokenizer,
                                            dataset[predict_set],
                                            examples[predict_set],
                                            features[predict_set])
                            }
            all_eval_sets_results[predict_set] = results
            logger.info("The evaluation for %s dataset is finished.", predict_set)
    logger.info("Results: {}".format(all_eval_sets_results))
    return all_eval_sets_results


@task(name="train", state_handlers=[post_to_slack])
def train_task(args, model, tokenizer, train_dataset):
    """Train the model on using the training dataset

    Parameters
    ----------
    args : tuple
        A tuple including the ModelArguments (doggmentator.trainer.arguments.ModelArguments) and TrainingArguments (transformers.training_args.TrainingArguments). Specifically, the following arguments from the ModelArguments are used in this function:
        - model_name_or_path : str
              Path to pretrained model or model identifier from huggingface.co/models
        The following arguments from the TrainingArguments are used in this function:
        - output_dir : str
              The output directory where the model predictions and checkpoints will be written.

    Returns
    -------
    This function does not return anything. It trains the model and save it.
    """
    model_args, training_args = args

    # Initialize the Trainer
    trainer = Trainer(
        model_args=model_args,
        data_collator=None,
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        train_dataset=train_dataset,
        prediction_loss_only=True,
    )

    # Training
    trainer.train(
        model_path=model_args.model_name_or_path if os.path.isdir(model_args.model_name_or_path) else None
    )
    trainer.save_model()
    # For convenience, we also re-save the tokenizer to the same directory,
    # so that you can share your model easily on huggingface.co/models =)
    if trainer.is_world_master():
        tokenizer.save_pretrained(training_args.output_dir)


def build_flow(args, label: str='default-train', model=None, tokenizer=None, train_dataset=None):
    model_args, training_args = args
    with Flow(label) as f:
        if training_args.do_eval and training_args.do_train:
            res = eval_task(
                    (model_args, training_args),
                    upstream_tasks=[
                        train_task(
                            (model_args, training_args),
                            model,
                            tokenizer,
                            train_dataset
                        )
                    ]
                )
        elif training_args.do_eval:
            res = eval_task((model_args, training_args))
        elif training_args.do_train:
            res = train_task(
                        (model_args, training_args),
                        model,
                        tokenizer,
                        train_dataset
                    )
        else:
            f = None
            logging.error('Flow must be instantiated with at least one of \"do_train\", \"do_eval\"')
    return f

def is_apex_available():
    try:
        from apex import amp  # noqa: F401
        _has_apex = True
    except ImportError:
        _has_apex = False
    return _has_apex


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)
