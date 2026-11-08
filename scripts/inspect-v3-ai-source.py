#!/usr/bin/env python3
"""Screen exact Git source for V3 connection capabilities. Not runtime or registry proof."""
import argparse,ast,hashlib,json,re,subprocess
from pathlib import Path
from copy import deepcopy
FILES=['app/jobs/config.py','app/core/config.py','app/jobs/kafka.py','app/infra/qdrant.py','app/infra/place_index.py','app/jobs/runner.py']
SECURITY_OPTIONS = {'ssl_context', 'sasl_mechanism', 'sasl_plain_username', 'sasl_plain_password'}


class UnsupportedOptions(ValueError):
    """A dynamic mutation cannot establish the returned mapping's keys safely."""


def _mapping_keys(expression, bindings):
    if isinstance(expression, ast.Name):
        return bindings.get(expression.id)
    if isinstance(expression, ast.Dict):
        keys = set()
        for key, value in zip(expression.keys, expression.values):
            if any(isinstance(n, ast.Name) and bindings.get(n.id) is not None for n in ast.walk(value)):
                raise UnsupportedOptions('Mapping used in a dictionary value')
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                raise UnsupportedOptions('Only literal dictionary keys are supported')
            if isinstance(value, ast.Constant) and value.value is None:
                keys.discard(key.value)
            else:
                keys.add(key.value)
        return keys
    return None


def _sasl_condition(expression, parameter):
    """Evaluate only constants and comparisons of this function's Kafka protocol.

    We inspect the SASL_SSL path: PLAINTEXT is allowed to return fewer options.
    Unknown conditions fork rather than assuming their guarded update executes.
    """
    unknown = object()

    def operand(node):
        if isinstance(node, ast.Constant):
            return node.value
        if (isinstance(node, ast.Attribute) and node.attr == 'KAFKA_SECURITY_PROTOCOL'
                and isinstance(node.value, ast.Name) and node.value.id == parameter):
            return 'SASL_SSL'
        return unknown

    if isinstance(expression, ast.Constant):
        return bool(expression.value)
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        value = _sasl_condition(expression.operand, parameter)
        return None if value is None else not value
    if isinstance(expression, ast.Compare) and len(expression.ops) == 1:
        left, right = operand(expression.left), operand(expression.comparators[0])
        if left is not unknown and right is not unknown:
            if isinstance(expression.ops[0], ast.Eq):
                return left == right
            if isinstance(expression.ops[0], ast.NotEq):
                return left != right
    return None


def kafka_returns_security_options(function):
    """Bounded key-flow screen, not a Python interpreter or TLS execution proof.

    Support literal dictionaries, local aliases and
    dict.update(dict_literal / keyword_arguments). Unsupported mutations,
    escaped mappings, dynamic unpacking, loops and excessive branching block.
    Never import or execute the inspected AI program.
    """
    if function is None or function.decorator_list:
        return False
    if any(isinstance(n, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)) for n in ast.walk(function)):
        return False
    arguments = function.args.posonlyargs + function.args.args
    parameter = arguments[0].arg if arguments else None
    returns = []
    visits = 0

    def escapes(expression, bindings):
        return any(isinstance(n, ast.Name) and bindings.get(n.id) is not None
                   for n in ast.walk(expression))

    def mapping_call(expression, bindings):
        return any(isinstance(n, ast.Call) and escapes(n, bindings)
                   for n in ast.walk(expression)) if expression is not None else False

    def assign(target, value, bindings):
        if isinstance(target, ast.Name):
            bindings[target.id] = value
        else:
            raise UnsupportedOptions('Unsupported assignment target')

    def walk(statements, states, depth=0):
        nonlocal visits
        if depth > 12:
            raise UnsupportedOptions('Nested control flow exceeds screen limit')
        for statement in statements:
            if not states:
                break
            next_states = []
            for bindings in states:
                visits += 1
                if visits > 512:
                    raise UnsupportedOptions('Control flow exceeds screen limit')
                if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    value = statement.value
                    if isinstance(statement, ast.AnnAssign) and value is None:
                        next_states.append(bindings)
                        continue
                    if isinstance(statement, ast.Assign):
                        targets = statement.targets
                    else:
                        targets = [statement.target]
                    if mapping_call(value, bindings):
                        raise UnsupportedOptions('Mapping passed to an unknown function')
                    keys = _mapping_keys(value, bindings)
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id == parameter:
                            raise UnsupportedOptions('Protocol input is reassigned')
                        assign(target, keys, bindings)
                    next_states.append(bindings)
                elif isinstance(statement, ast.Expr):
                    call = statement.value
                    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                            and isinstance(call.func.value, ast.Name)
                            and call.func.value.id in bindings):
                        if bindings[call.func.value.id] is None:
                            raise UnsupportedOptions('Unknown local receiver mutation')
                        if call.func.attr != 'update' or len(call.args) > 1:
                            raise UnsupportedOptions('Unknown mapping mutation')
                        keys = bindings[call.func.value.id]
                        if call.args:
                            if not isinstance(call.args[0], ast.Dict):
                                raise UnsupportedOptions('Only a literal update mapping is supported')
                            other = _mapping_keys(call.args[0], bindings)
                            if other is None:
                                raise UnsupportedOptions('Unknown update input')
                            keys.update(other)
                            # Explicit None overwrites must not retain an earlier key.
                            final_values = {k.value: v for k, v in zip(call.args[0].keys, call.args[0].values)}
                            for key, value in final_values.items():
                                if isinstance(value, ast.Constant) and value.value is None:
                                    keys.discard(key)
                        for keyword in call.keywords:
                            if escapes(keyword.value, bindings):
                                raise UnsupportedOptions('Mapping used inside its update value')
                            if keyword.arg is None:
                                raise UnsupportedOptions('Dynamic keyword unpacking')
                            if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
                                keys.discard(keyword.arg)
                            else:
                                keys.add(keyword.arg)
                    elif mapping_call(call, bindings):
                        raise UnsupportedOptions('Mapping escapes local analysis')
                    next_states.append(bindings)
                elif isinstance(statement, ast.If):
                    if mapping_call(statement.test, bindings):
                        raise UnsupportedOptions('Mapping mutated in condition')
                    condition = _sasl_condition(statement.test, parameter)
                    if condition is not False:
                        next_states.extend(walk(statement.body, [deepcopy(bindings)], depth + 1))
                    if condition is not True:
                        next_states.extend(walk(statement.orelse, [deepcopy(bindings)], depth + 1))
                elif isinstance(statement, ast.Return):
                    keys = _mapping_keys(statement.value, bindings)
                    returns.append(set(keys) if keys is not None else set())
                elif isinstance(statement, ast.Raise):
                    pass  # Exception paths do not return connection options.
                elif isinstance(statement, ast.Assert):
                    if mapping_call(statement.test, bindings):
                        raise UnsupportedOptions('Mapping mutated in assertion')
                    if _sasl_condition(statement.test, parameter) is not False:
                        next_states.append(bindings)
                elif isinstance(statement, ast.Pass):
                    next_states.append(bindings)
                else:
                    raise UnsupportedOptions('Unsupported statement in connection options')
            if len(next_states) > 64:
                raise UnsupportedOptions('Too many possible return paths')
            states = next_states
        return states

    try:
        fallthrough = walk(function.body, [{}])
    except UnsupportedOptions:
        return False
    return bool(returns) and not fallthrough and all(SECURITY_OPTIONS <= keys for keys in returns)


def inspect(files,source):
    trees={k:ast.parse(v) for k,v in files.items()};settings='\n'.join(files[k] for k in FILES[:2])
    annotations={n.target.id:ast.unparse(n.annotation) for k in FILES[:2] for n in ast.walk(trees[k]) if isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name)}
    checks={'kafkaProtocol':'SASL_SSL' in annotations.get('KAFKA_SECURITY_PROTOCOL',''),'kafkaSettings':all(k in annotations for k in ['KAFKA_SASL_MECHANISM','KAFKA_SASL_USERNAME','KAFKA_SASL_PASSWORD','KAFKA_SSL_CAFILE'])}
    options=[n for n in trees['app/jobs/kafka.py'].body if isinstance(n,ast.FunctionDef) and n.name=='connection_options']
    option=options[0] if len(options)==1 else None
    checks['kafkaClientOptions']=kafka_returns_security_options(option)
    checks['qdrantSetting']='QDRANT_API_KEY' in annotations
    calls=[n for n in ast.walk(trees['app/infra/qdrant.py']) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in ['QdrantClient','AsyncQdrantClient'] and any(k.arg=='url' for k in n.keywords)]
    checks['qdrantClients']=len(calls)>=2 and all(any(k.arg=='api_key' and not (isinstance(k.value,ast.Constant) and k.value.value is None) for k in n.keywords) for n in calls)
    checks['placeIndexSettings']=all(k in annotations for k in ['PLACE_INDEX_PATH','PLACE_INDEX_SHA256','PLACE_INDEX_S3_URI','KAKAO_REST_API_KEY'])
    checks['runnerIndexPreparation']=any(isinstance(n,ast.Name) and n.id=='prepare_place_index' for n in ast.walk(trees['app/jobs/runner.py']))
    return {'kind':'v3-ai-source-screen-v1','source':source,'status':'compatible' if all(checks.values()) else 'blocked','checks':checks,'missing':[k for k,v in checks.items() if not v],'files':{k:hashlib.sha256(v.encode()).hexdigest() for k,v in files.items()},'runtimeVerified':False}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repository',type=Path,required=True);p.add_argument('--source',required=True);p.add_argument('--output',type=Path);a=p.parse_args()
    if not re.fullmatch('[a-f0-9]{40}',a.source):raise SystemExit('Exact source Commit required')
    files={k:subprocess.check_output(['git','-C',str(a.repository),'show',a.source+':'+k],text=True) for k in FILES};result=inspect(files,a.source)
    if a.output:a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'source':a.source,'status':result['status'],'missing':result['missing'],'runtimeVerified':False}))
    return 0 if result['status']=='compatible' else 1
if __name__=='__main__':raise SystemExit(main())
