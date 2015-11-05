import datetime
import logging
import math
import os
from hashlib import sha1

import simplejson as json
from jsonschema import validate

from treeherder.model.models import (MachinePlatform,
                                     OptionCollection,
                                     Repository)
from treeherder.perf.models import (PerformanceDatum,
                                    PerformanceFramework,
                                    PerformanceSignature)

logger = logging.getLogger(__name__)


PERFORMANCE_ARTIFACT_TYPES = set([
    'performance_data',
    'talos_data'
])


# keys useful for creating a non-redundant performance signature
SIGNIFICANT_REFERENCE_DATA_KEYS = ['option_collection_hash',
                                   'machine_platform']


PERFHERDER_SCHEMA = json.load(open(os.path.join('schemas',
                                                'performance-artifact.json')))
TALOS_SCHEMA = json.load(open(os.path.join('schemas',
                                           'talos-artifact.json')))


def _transform_signature_properties(properties, significant_keys=None):
    if significant_keys is None:
        significant_keys = SIGNIFICANT_REFERENCE_DATA_KEYS
    transformed_properties = {k: v for k, v in properties.iteritems() if
                              k in significant_keys}

    # HACK: determine if e10s is in job_group_symbol, and add an "e10s"
    # property to a 'test_options' property if so (we should probably
    # make talos produce this information somehow and consume it in the
    # future)
    if 'e10s' in properties.get('job_group_symbol', ''):
        transformed_properties['test_options'] = json.dumps(['e10s'])

    return transformed_properties


def _get_signature_hash(signature_properties):
    signature_prop_values = signature_properties.keys()
    str_values = []
    for value in signature_properties.values():
        if not isinstance(value, basestring):
            str_values.append(json.dumps(value))
        else:
            str_values.append(value)
    signature_prop_values.extend(str_values)

    sha = sha1()
    sha.update(''.join(map(lambda x: str(x), sorted(signature_prop_values))))

    return sha.hexdigest()


def load_perf_artifacts(project_name, reference_data, job_data, datum):
    blob = json.loads(datum['blob'])
    perf_datum = blob['performance_data']
    validate(perf_datum, PERFHERDER_SCHEMA)

    if 'e10s' in reference_data.get('job_group_symbol', ''):
        extra_properties = {'test_options': ['e10s']}
    else:
        extra_properties = {}

    # transform the reference data so it only contains what we actually
    # care about (for calculating the signature hash reproducibly), then
    # get the associated models
    reference_data = _transform_signature_properties(reference_data)
    option_collection = OptionCollection.objects.get(
        option_collection_hash=reference_data['option_collection_hash'])
    # there may be multiple machine platforms with the same platform: use
    # the first
    platform = MachinePlatform.objects.filter(
        platform=reference_data['machine_platform'])[0]
    repository = Repository.objects.get(
        name=project_name)

    # data for performance series
    job_guid = datum["job_guid"]
    job_id = job_data[job_guid]['id']
    result_set_id = job_data[job_guid]['result_set_id']
    push_timestamp = datetime.datetime.fromtimestamp(
        job_data[job_guid]['push_timestamp'])

    framework = PerformanceFramework.objects.get(name=perf_datum['framework']['name'])
    for suite in perf_datum['suites']:
        subtest_signatures = []
        for subtest in suite['subtests']:
            subtest_properties = {
                'suite': suite['name'],
                'test': subtest['name']
            }
            subtest_properties.update(reference_data)
            subtest_signature_hash = _get_signature_hash(
                subtest_properties)
            subtest_signatures.append(subtest_signature_hash)

            signature, _ = PerformanceSignature.objects.update_or_create(
                repository=repository,
                signature_hash=subtest_signature_hash,
                defaults={
                    'test': subtest['name'],
                    'suite': suite['name'],
                    'option_collection': option_collection,
                    'platform': platform,
                    'framework': framework,
                    'extra_properties': extra_properties,
                    'lower_is_better': subtest.get('lowerIsBetter', True)
                })
            PerformanceDatum.objects.get_or_create(
                repository=repository,
                result_set_id=result_set_id,
                job_id=job_id,
                signature=signature,
                push_timestamp=push_timestamp,
                defaults={'value': subtest['value']})

        # if we have a summary value, create or get its signature and insert
        # it too
        if suite.get('value') is not None:
            # summary series
            extra_summary_properties = {
                'subtest_signatures': sorted(subtest_signatures)
            }
            extra_summary_properties.update(extra_properties)
            summary_properties = {'suite': suite['name']}
            summary_properties.update(reference_data)
            summary_properties.update(extra_summary_properties)
            summary_signature_hash = _get_signature_hash(
                summary_properties)

            signature, _ = PerformanceSignature.objects.get_or_create(
                repository=repository, signature_hash=summary_signature_hash,
                defaults={
                    'test': '',
                    'suite': suite['name'],
                    'option_collection': option_collection,
                    'platform': platform,
                    'framework': framework,
                    'extra_properties': extra_summary_properties,
                    'last_updated': push_timestamp
                })
            PerformanceDatum.objects.get_or_create(
                repository=repository,
                result_set_id=result_set_id,
                job_id=job_id,
                signature=signature,
                push_timestamp=push_timestamp,
                defaults={'value': suite['value']})


def _calculate_summary_value(results):
    # needed only for legacy talos blobs which don't provide a suite
    # summary value
    values = []
    for test in results:
        values += results[test]

    if values:
        return math.exp(sum(map(lambda v: math.log(v+1),
                                values))/len(values))-1

    return 0.0


def _calculate_test_value(replicates):
    # needed only for legacy talos blobs which don't provide a test
    # summary value
    replicates.sort()
    r = replicates
    r_len = len(replicates)

    value = 0.0

    if r_len > 0:
        def avg(s):
            return float(sum(s)) / len(s)

        value = avg(r)

        if r_len > 1:
            if len(r) % 2 == 1:
                value = r[int(math.floor(len(r)/2))]
            else:
                value = avg([r[(len(r)/2) - 1], r[len(r)/2]])

    return value


def load_talos_artifacts(project_name, reference_data, job_data, datum):
    if 'e10s' in reference_data.get('job_group_symbol', ''):
        extra_properties = {'test_options': ['e10s']}
    else:
        extra_properties = {}

    # transform the reference data so it only contains what we actually
    # care about (for calculating the signature hash reproducibly), then
    # get the associated models
    reference_data = _transform_signature_properties(reference_data)
    option_collection = OptionCollection.objects.get(
        option_collection_hash=reference_data['option_collection_hash'])
    framework = PerformanceFramework.objects.get(name='talos')
    # there may be multiple machine platforms with the same platform: use
    # the first
    platform = MachinePlatform.objects.filter(
        platform=reference_data['machine_platform'])[0]
    repository = Repository.objects.get(
        name=project_name)

    # Get just the talos datazilla structure for treeherder
    target_datum = json.loads(datum['blob'])
    for talos_datum in target_datum['talos_data']:
        validate(talos_datum, TALOS_SCHEMA)
        _job_guid = datum["job_guid"]
        _suite = talos_datum["testrun"]["suite"]

        # data for performance series
        job_id = job_data[_job_guid]['id']
        result_set_id = job_data[_job_guid]['result_set_id']
        push_timestamp = datetime.datetime.fromtimestamp(
            job_data[_job_guid]['push_timestamp'])

        # counters will not be part of the summary series
        # counters have a json obj {'stat': val} instead of [val1, val2, ...]
        if 'talos_counters' in talos_datum:
            for _test in talos_datum["talos_counters"].keys():
                signature_properties = {
                    'suite': _suite,
                    'test': _test
                }
                signature_properties.update(reference_data)
                signature_properties.update(extra_properties)
                signature_hash = _get_signature_hash(
                    signature_properties)

                signature, _ = PerformanceSignature.objects.get_or_create(
                    repository=repository, signature_hash=signature_hash,
                    defaults={
                        'test': _test,
                        'suite': _suite,
                        'option_collection': option_collection,
                        'platform': platform,
                        'framework': framework,
                        'extra_properties': extra_properties,
                        'last_updated': push_timestamp
                    })

                try:
                    value = float(
                        talos_datum["talos_counters"][_test]["mean"])
                except:
                    logger.warning("Talos counters for job %s, "
                                   "result_set %s, and counter named %s "
                                   "have an unexpected value: %s" %
                                   (job_id, result_set_id, _test,
                                    talos_datum["talos_counters"][_test]))
                    continue

                PerformanceDatum.objects.get_or_create(
                    repository=repository,
                    result_set_id=result_set_id,
                    job_id=job_id,
                    signature=signature,
                    push_timestamp=push_timestamp,
                    defaults={'value': value})

        subtest_signatures = []

        # series for all the subtests
        for _test in talos_datum["results"].keys():

            signature_properties = {
                'suite': _suite,
                'test': _test
            }
            signature_properties.update(reference_data)

            signature_hash = _get_signature_hash(
                signature_properties)
            subtest_signatures.append(signature_hash)

            if "summary" in talos_datum:
                # most talos results should provide a summary of their
                # subtest results based on an internal calculation of
                # the replicates, use that if available
                testdict = talos_datum["summary"]["subtests"][_test]
                value = testdict["filtered"]
                # "lower is better" is a property than can change
                lower_is_better = testdict.get('lowerIsBetter', True)
            else:
                # backwards compatibility for older versions of talos
                # and android talos which don't provide this summary
                # (at some point we can remove this)
                value = _calculate_test_value(
                    talos_datum["results"][_test])
                lower_is_better = True

            signature, _ = PerformanceSignature.objects.update_or_create(
                repository=repository, signature_hash=signature_hash,
                defaults={
                    'test': _test,
                    'suite': _suite,
                    'option_collection': option_collection,
                    'platform': platform,
                    'framework': framework,
                    'extra_properties': extra_properties,
                    'lower_is_better': lower_is_better
                })

            PerformanceDatum.objects.get_or_create(
                repository=repository,
                result_set_id=result_set_id,
                job_id=job_id,
                signature=signature,
                push_timestamp=push_timestamp,
                defaults={'value': value})

        if subtest_signatures and len(subtest_signatures) > 1:
            # summary series
            extra_summary_properties = {
                'subtest_signatures': sorted(subtest_signatures)
            }
            extra_summary_properties.update(extra_properties)
            summary_properties = {'suite': _suite}
            summary_properties.update(reference_data)
            summary_properties.update(extra_summary_properties)
            summary_signature_hash = _get_signature_hash(
                summary_properties)

            if "summary" in talos_datum and "suite" in talos_datum["summary"]:
                value = talos_datum["summary"]["suite"]
                lower_is_better = talos_datum["summary"].get("lowerIsBetter",
                                                             True)
            else:
                value = _calculate_summary_value(talos_datum["results"])
                lower_is_better = True
            signature, _ = PerformanceSignature.objects.update_or_create(
                repository=repository, signature_hash=summary_signature_hash,
                defaults={
                    'test': '',
                    'suite': _suite,
                    'option_collection': option_collection,
                    'platform': platform,
                    'framework': framework,
                    'extra_properties': extra_summary_properties,
                    'lower_is_better': lower_is_better
                })

            PerformanceDatum.objects.get_or_create(
                repository=repository,
                result_set_id=result_set_id,
                job_id=job_id,
                signature=signature,
                push_timestamp=push_timestamp,
                defaults={'value': value})
