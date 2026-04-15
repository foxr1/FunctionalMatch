__author__ = "Giacomo Bergami"
__copyright__ = "Copyright 2025, Functional Match"
__credits__ = ["Giacomo Bergami"]
__license__ = "GPLv3"
__version__ = "2.0"
__maintainer__ = "Giacomo Bergami"
__email__ = "bergamigiacomo@gmail.com"
__status__ = "Production"

from curses.ascii import isdigit
# from collections import defaultdict
from dataclasses import dataclass, is_dataclass, fields
from typing import Optional, List, Tuple

import dacite

# from FunctionalMatch import structural_match
from FunctionalMatch.PropositionalLogic import Prop, var_interpret, var_update, ExternalPredicateByExtesion
from FunctionalMatch.TransformationResults import ReplaceWith
from FunctionalMatch.functions.structural_match import Variable, var, JSONPath
from FunctionalMatch.utils import FrozenDict


@dataclass(eq=True, order=True, frozen=True)
class ExternalMatchByExtesion:
    function_name: str
    module: str
    extra_args: Optional[FrozenDict] = None
    packed_call: Optional[object] = None ## TODO: how to call the function when an argument is provided

    def asPredicate(self):
        return ExternalPredicateByExtesion(self.module, self.function_name, self.extra_args)

    def with_extra_args(self, args):
        if args is None:
            return self
        elif isinstance(args, dict):
            args = FrozenDict.from_dictionary(args)
        elif not isinstance(args, FrozenDict):
            return self
        return ExternalMatchByExtesion(self.function_name, self.module, args, self.packed_call)

    def add_packed_args(self, element):
        return ExternalMatchByExtesion(self.function_name, self.module, self.extra_args, element)

    def structural_map(self, f):
        extra_args = {f(k): f(v) for k, v in self.extra_args.items()} if self.extra_args is not None else dict()
        packed_call = f(self.packed_call) if self.packed_call is not None else None
        return ExternalMatchByExtesion(self.function_name, self.module, FrozenDict.from_dictionary(extra_args), packed_call)

    def callMe(self):
        return self(self.packed_call)

    def interpretation(self, kwargs):
        return self.__call__(kwargs)

    def __call__(self, x):
        import importlib
        mod = importlib.import_module(self.module) #__import__(self.module)
        func = getattr(mod, self.function_name)
        return func(x) if self.extra_args is None else func(x, **self.extra_args.dict())

@dataclass(eq=True, order=True, frozen=True)
class MatchMemo:
    target_id: int
    jsonpath: str

def evaluate_structural_function(obj):
    if obj is None:
        return None
    elif isinstance(obj, list):
        return [evaluate_structural_function(x) for x in obj]
    elif isinstance(obj, tuple):
        return tuple(evaluate_structural_function(x) for x in obj)
    elif isinstance(obj, dict):
        return {evaluate_structural_function(k): evaluate_structural_function(v) for k, v in obj.items()}
    elif isinstance(obj, FrozenDict):
        return FrozenDict.from_dictionary({evaluate_structural_function(k): evaluate_structural_function(v) for k, v in obj.items()})
    elif is_dataclass(obj):
        if isinstance(obj, ExternalMatchByExtesion) or type(obj).__name__ == "ExternalMatchByExtesion":
            return obj.structural_map(evaluate_structural_function).callMe()
        else:
            d = dict()
            for field in fields(obj):
                d[field.name] = evaluate_structural_function(getattr(obj, field.name))
            return dacite.from_dict(type(obj), d, dacite.Config(check_types=False))
    else:
        return obj

def doesContainExternalMatch(obj):
    if obj is None:
        return False
    elif isinstance(obj, list) or isinstance(obj, tuple):
        return any(map(doesContainExternalMatch, obj))
    elif isinstance(obj, dict) or isinstance(obj, FrozenDict):
        return any(map(doesContainExternalMatch, obj.values())) or any(map(doesContainExternalMatch, obj.keys()))
    elif is_dataclass(obj):
        if isinstance(obj, ExternalMatchByExtesion) or type(obj).__name__ == "ExternalMatchByExtesion":
            return True
        else:
            for field in fields(obj):
                if doesContainExternalMatch(getattr(obj, field.name)):
                    return True
            return False
    else:
        return False

@dataclass(eq=True, order=True, frozen=True)
class Match:
    query: tuple
    nested: bool
    where: Optional[Prop]
    extension: Tuple[ExternalMatchByExtesion]
    replacement: ReplaceWith

    @property
    def matching_obj_vars(self):
        return {f"${x}" for x in range(len(self.query))}

    def structural_match_single_query(self, query, target, number):
        outcome = []
        from FunctionalMatch.functions.structural_match import _structural_match
        result = _structural_match(query, target, dict())
        # current = f"${number}"
        if result is not None:
            result[number] = target
            outcome.append(result)
        if self.nested and (result is not None) and is_dataclass(target):
            for k in fields(target):
                    x = getattr(target, k.name)
                    do_extend, dd = self.structural_match(query, x,f"{number}.{k.name}") ## TODO: revise recursive call after update
                    if do_extend and len(dd) > 0:
                        outcome += dd
        # TODO:
        #  if self.nested: #and (result is not None)
        #     if is_dataclass(target):
        #         for k in fields(target):
        #                 x = getattr(target, k.name)
        #                 do_extend, dd = self.structural_match(query, x,f"{number}.{k.name}") ## TODO: revise recursive call after update
        #                 if do_extend and len(dd) > 0:
        #                     outcome += dd
        #     elif isinstance(target, list) or isinstance(target, tuple):
        #         for idx, x in enumerate(target):
        #             do_extend, dd = self.structural_match(query, x,
        #                                                   f"{number}[{idx}]")  ## TODO: revise recursive call after update
        #             if do_extend and len(dd) > 0:
        #                 outcome += dd
        #     else:
        #         try:
        #             d = dict(target)
        #         except:
        #             d = None
        #         if isinstance(d, dict):
        #             for k, x in d.items():
        #                 do_extend, dd = self.structural_match(query, x,f"{number}.{k}") ## TODO: revise recursive call after update
        #                 if do_extend and len(dd) > 0:
        #                     outcome += dd
        return outcome

    def structural_match(self, query, target, number):
        outcome = self.structural_match_single_query(query, target, number)
        return len(outcome) > 0, outcome

    def structural_match_main_loop(self, targets):
        results = dict()
        for entry_idx, x in enumerate(targets):
            for query_idx, q in enumerate(self.query):
                if doesContainExternalMatch(q):
                    raise RuntimeError(f"ERROR: query {query_idx} contains a function call, which is not allowed at the matching phase!")
                cartouche = f"${query_idx}@{entry_idx}"
                test, outcome = self.structural_match(q, x, f"{cartouche}:$")
                if test:
                    if query_idx not in results:
                        results[query_idx] = list() #dict()
                    if len(outcome) > 0:
                        results[query_idx].extend(outcome) #[entry_idx]

        ## All the queries should have at least one match. If this does not happen, then no sensible result can be obtained
        if len(results) != len(self.query):
            return False, []

        ## The actual equijoin boils down to equi-join over the values, so to derive different possible matches
        from FunctionalMatch.functions.structural_match import equi_join_dictionaries
        outcome = equi_join_dictionaries(list(results.values()))

        outcome_mapping = [{k[:k.find("@")]: MatchMemo(int(k[k.find("@")+1:k.find(":")]), k[k.find(":")+1:]) for k in out.keys() if k.startswith("$") and k.find("@")>0 and k.find(":")>0 and k.find("@")<k.find(":")} for out in outcome]
        outcome =         [{k[:k.find("@")] if k.startswith("$") and k.find("@")>0 and k.find(":")>0 and k.find("@")<k.find(":") else k: v for k,v in out.items()} for out in outcome]

        assert len(outcome) == len(outcome_mapping)
        if self.replacement is not None:
            outcome = [self.replacement(obj) for obj in outcome]
        # else:
        #     return test, outcome

        outcome_to_outcome_mapping = dict()
        if (self.extension is not None) and (
                isinstance(self.extension , list) or isinstance(self.extension ,
                                                                              tuple)) and all(
                map(callable, self.extension )) and len(self.extension ) > 0:
            if self.extension is not None and len(self.extension) > 0:
                tmp = []
                Q = [(idx,current,0) for idx, current in enumerate(outcome)]
                while len(Q) > 0:
                    outcome_idx, curr, ext_id = Q.pop()
                    fun = self.extension[ext_id]
                    curr = curr if isinstance(curr, FrozenDict) else FrozenDict.from_dictionary(curr)
                    result = fun(curr)
                    if result is None:
                        continue
                    if ext_id+1 < len(self.extension):
                        if isinstance(result, list):
                            for x in result:
                                Q.append((outcome_idx, x, ext_id+1))
                        else:
                            Q.append((outcome_idx, result, ext_id+1 ))
                    else:
                        if isinstance(result, list):
                            for x in result:
                                outcome_to_outcome_mapping[len(tmp)] = outcome_idx
                                tmp.append(x)
                        else:
                            outcome_to_outcome_mapping[len(tmp)] = outcome_idx
                            tmp.append(result)
                # for x in outcome:
                #     for extension_match_function in self.extension:
                #         x = extension_match_function(x if isinstance(x, FrozenDict) else FrozenDict.from_dictionary(x))
                #     tmp.append(x)
                outcome = tmp
            else:
                outcome_to_outcome_mapping = {i:i for i in range(len(outcome_mapping))}
        else:
            outcome_to_outcome_mapping = {i: i for i in range(len(outcome_mapping))}
        if self.where is not None:
            from FunctionalMatch.functions.Where import where
            from operator import itemgetter
            test, idxs = where(outcome, self.where)
            if len(outcome)>0 and len(idxs)>0:
                outcome = [outcome[i] for i in idxs]
                outcome_mapping = [outcome_mapping[outcome_to_outcome_mapping[i]] for i in idxs]
            else:
                outcome = []
                outcome_mapping = []
        else:
            test = len(outcome) > 0
            if test:
                outcome_mapping = [outcome_mapping[outcome_to_outcome_mapping[i]] for i in outcome_to_outcome_mapping.keys()]
        return test, list(zip(outcome, outcome_mapping))

    def __call__(self, x):
        if isinstance(x, list) or isinstance(x, tuple) or isinstance(x, set):
            return self.structural_match_main_loop(list(x))
        elif isinstance(x, dict):
            raise RuntimeError("Unsupported structure match (dict)")
        else:
            return self.structural_match_main_loop([x])

def value_extraction_for_rewriting(dictionary, value):
    if isinstance(value, Variable) and value.name in dictionary:
        return dictionary[value.name]
    elif isinstance(value, JSONPath):
        return var_interpret(value, dictionary)
    elif is_dataclass(value):
        from FunctionalMatch.functions.Instantiate import instantiate
        return instantiate(value, dictionary)
    else:
        return None

def rewrite_as(dictionary:FrozenDict, rewriting_rules):
    assert isinstance(dictionary, FrozenDict)
    assert isinstance(rewriting_rules, tuple)
    for key, value in rewriting_rules:
        if isinstance(key, str):
            key = var(key)
        if isinstance(value, str):
            value = var(value)
        result = value_extraction_for_rewriting(dictionary, value)
        if result is not None:
            if isinstance(key, JSONPath):
                dictionary = var_update(result, key, dictionary)
            elif isinstance(key, Variable):
                target_key = key.name
                dictionary = dictionary.update(target_key, result)
    return dictionary




