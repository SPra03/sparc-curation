import copy
import json
from functools import wraps
import jsonschema

# FIXME these imports should not be here types rules should be set in another way
from pathlib import Path
import idlib
import rdflib
import requests
from pyontutils.core import OntId, OntTerm
from pysercomb.pyr.units import Expr
from sparcur import exceptions as exc
from sparcur.utils import logd
from sparcur.core import JEncode


class hproperty:
    def __init__(self, fget=None, fset=None, fdel=None, doc=None):
        self.fget = fget
        self.fset = fset
        self.fdel = fdel
        if doc is None and fget is not None:
            doc = fget.__doc__
        self.__doc__ = doc

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self

        if self.fget is None:
            raise AttributeError("unreadable attribute")

        return self.fget(obj)

    def __set__(self, obj, value):
        if self.fset is None:
            raise AttributeError("can't set attribute")

        self.fset(obj, value)

    def __delete__(self, obj):
        if self.fdel is None:
            raise AttributeError("can't delete attribute")

        self.fdel(obj)

    def getter(self, fget):
        return type(self)(fget, self.fset, self.fdel, self.__doc__)

    def setter(self, fset):
        return type(self)(self.fget, fset, self.fdel, self.__doc__)

    def deleter(self, fdel):
        return type(self)(self.fget, self.fset, fdel, self.__doc__)


class HasSchema:
    """ decorator for classes with methods whose output can be validated by jsonschema """
    def __init__(self, input_schema_class=None, fail=True, normalize=False):
        self.input_schema = input_schema_class() if input_schema_class is not None else None
        self.fail = fail
        self.normalize = normalize
        self.schema = None  # deprecated output schema ...

    def mark(self, cls):
        """ note that this runs AFTER all the methods """

        if self.input_schema is not None:
            # FIXME probably better to do this as types
            # and check that schemas match at class time
            cls._pipeline_start = cls.pipeline_start
            @hproperty
            def pipeline_start(self, schema=self.input_schema, fail=self.fail):
                data = self._pipeline_start
                ok, norm_or_error, data = schema.validate(data)
                if not ok and fail:
                    raise norm_or_error

                return data

            pipeline_start.schema = self.input_schema
            cls.pipeline_start = pipeline_start

        return cls

        # pretty sure this functionality is no longer used
        if self.schema is not None:
            cls._output = cls.output
            @property
            def output(_self):
                return self.schema.validate(cls._output)

            cls.output = output

        return cls

    def f(self, schema_class, fail=False):
        return self(schema_class, fail=fail, _property=False)

    def __call__(self, schema_class, fail=False, _property=True):
        # TODO switch for normalized output if value passes?
        schema = schema_class()
        def decorator(function):
            pipeline_stage_name = function.__qualname__
            @wraps(function)
            def schema_wrapped_property(_self, *args, **kwargs):
                data = function(_self, *args, **kwargs)
                ok, norm_or_error, data = schema.validate(data)
                if not ok:
                    if fail:
                        logd.error('schema validation has failed and fail=True')
                        raise norm_or_error

                    try:
                        if 'errors' not in data:
                            data['errors'] = []
                    except BaseException as e:
                        raise exc.SparCurError(
                            f'Error from {_self.__class__.__name__}.'
                            f'{function.__name__}') from e
                        
                    data['errors'] += norm_or_error.json(pipeline_stage_name)
                    # TODO make sure the step is noted even if the schema is the same
                elif self.normalize:
                    return norm_or_error

                return data

            if _property:
                schema_wrapped_property = hproperty(schema_wrapped_property)

            schema_wrapped_property.schema = schema
            return schema_wrapped_property
        return decorator


class ConvertingChecker(jsonschema.FormatChecker):
    _enc = JEncode()

    def check(self, instance, format):
        converted = _enc.default(instance)
        return super().check(converted, format)


class JSONSchema(object):

    schema = {}

    validator_class = jsonschema.Draft6Validator

    string_types = [  # add more types here
        str,
        Path,
        idlib.Stream,
        rdflib.URIRef,
        rdflib.Literal,
        OntId,
        OntTerm,
        Expr,
    ]

    def __init__(self):
        format_checker = jsonschema.FormatChecker()
        #format_checker = ConvertingChecker()
        types = dict(array=(list, tuple),
                     string=tuple(self.string_types))
        self.validator = self.validator_class(self.schema,
                                              format_checker=format_checker,
                                              types=types)

    @classmethod
    def _add_meta(cls, blob):
        """ In place modification of a schema blob to add schema level metadata """
        # TODO schema metadata, I don't know enough to do this correctly yet
        #schema_version = cls.validator_class.ID_OF(cls.validator_class.META_SCHEMA)
        #schema['$schema'] = schema_version

        #schema_id = '#/temp/' + cls.__name__ + '.json'  # FIXME version ...
        #schema['$id'] = schema_id

    @classmethod
    def export(cls, base_path):
        """ write schema to file with """
        def pop_errors(dict_):
            if 'errors' in dict_:
                dict_.pop('errors')
            for v in dict_.values():
                if isinstance(v, dict):
                    pop_errors(v)

        schema = copy.deepcopy(cls.schema)
        pop_errors(schema)
        cls._add_meta(schema)

        with open((base_path / cls.__name__).with_suffix('.json'), 'wt') as f:
            json.dump(schema, f, sort_keys=True, indent=2)

    def validate_strict(self, data):
        # Take a copy to ensure we don't modify what we were passed.
        #appstruct = copy.deepcopy(data)

        appstruct = json.loads(json.dumps(data, cls=JEncode))  # FIXME figure out converters ...

        errors = list(self.validator.iter_errors(appstruct))
        if errors:
            raise exc.ValidationError(errors)

        return appstruct

    def validate(self, data):
        """ capture errors """
        try:
            ok = self.validate_strict(data)  # validate {} to get better error messages
            return True, ok, data  # FIXME better format

        except exc.ValidationError as e:
            return False, e, data  # FIXME better format


class RemoteSchema(JSONSchema):
    schema = ''
    def __new__(cls):
        if isinstance(cls.schema, str):
            cls.schema = requests.get(cls.schema).json()

        return super().__new__(cls)


class ApiNATOMYSchema(RemoteSchema):
    schema = ('https://raw.githubusercontent.com/open-physiology/'
              'open-physiology-viewer/master/src/model/graphScheme.json')


metadata_filename_pattern = r'^.+\/[a-z_\/]+\.(xlsx|csv|tsv|json)$'

simple_url_pattern = r'^(https?):\/\/([^\s\/]+)\/([^\s]*)'

iso8601pattern = '^[0-9]{4}-[0-1][0-9]-[0-3][0-9]T[0-2][0-9]:[0-6][0-9]:[0-6][0-9](,[0-9]{6})*(Z|[-\+[0-2][0-9]:[0-6][0-9]])'

# https://www.crossref.org/blog/dois-and-matching-regular-expressions/
doi_pattern = '^https:\/\/doi.org/10\.[0-9]{4,9}[-._;()/:a-zA-Z0-9]+$'

class ErrorSchema(JSONSchema):
    schema = {'type':'array',
              'minItems': 1,
              'items': {'type': 'object'},}


class ProvSchema(JSONSchema):
    schema = {'type': 'object',
              'properties': {'timestamp_export_start': {'type': 'string',
                                                        'pattern': iso8601pattern,},
                             #'timestamp_export_end': {'type': 'string'},  # this isn't really possible
                             'export_system_identifier': {'type': 'string'},
                             'export_system_hostname': {'type': 'string'},
                             'export_project_path': {'type': 'string'},
                             'sparcur_version': {'type': 'string'},
                             'sparcur_commit': {'type': 'string'},
                             'ontology_mappings': {
                                 'type': 'array',
                                 'minItems': 1,
                                 'items': {
                                     'type': 'object',
                                     'properties': {
                                         'curie': {'type': 'string'},
                                         'label': {'type': 'string'},
                                         'matches': {
                                             'type': 'array',
                                             'minItems': 1,
                                             'items': {
                                                 'type': 'object',
                                                 'properties': {
                                                     'path': {'type': 'array'},
                                                     'input': {'type': 'string'},
                                                 },},},},},},},}


class DatasetStructureSchema(JSONSchema):
    __schema = {'type': 'object',
                'required': ['submission_file', 'dataset_description_file'],
                'properties': {'submission_file': {'type': 'string',
                                                   'pattern': metadata_filename_pattern},
                               'dataset_description_file': {'type': 'string',
                                                            'pattern': metadata_filename_pattern},
                               'subjects_file': {'type': 'string',
                                                 'pattern': metadata_filename_pattern},
                               'samples_file': {'type': 'string',
                                                'pattern': metadata_filename_pattern},
                               #'path_structure': {'type': 'array',
                                                 # 'items': {'type': 'string', 'pattern': ''}}
                               'dirs': {'type': 'integer',
                                        'minimum': 0},
                               'files': {'type': 'integer',
                                         'minimum': 0},
                               'size': {'type': 'integer',
                                        'minimum': 0},
                               'errors': ErrorSchema.schema,
                }}
    schema = {'allOf': [__schema,
                        {'anyOf': [
                            {'required': ['subjects_file']},
                            {'required': ['samples_file']}]}]}


class ContributorSchema(JSONSchema):
    schema = {'type': 'object',
              'properties': {
                  'contributor_name': {'type': 'string'},
                  'first_name': {'type': 'string'},
                  'last_name': {'type': 'string'},
                  'contributor_orcid_id': {'type': 'string',
                                           'pattern': ('^https://orcid.org/0000-000(1-[5-9]|2-[0-9]|3-'
                                                       '[0-4])[0-9][0-9][0-9]-[0-9][0-9][0-9]([0-9]|X)$')},
                  'contributor_affiliation': {'type': 'string'},
                  'contributor_role': {
                      'type':'array',
                      'minItems': 1,
                      'items': {
                          'type': 'string',
                          'enum': ['ContactPerson',
                                   'Creator',  # allowed here, moved later
                                   'DataCollector',
                                   'DataCurator',
                                   'DataManager',
                                   'Distributor',
                                   'Editor',
                                   'HostingInstitution',
                                   'PrincipalInvestigator',  # added for sparc map to ProjectLeader probably?
                                   'CoInvestigator', # added for sparc, to distingusih ResponsibleInvestigator
                                   'Producer',
                                   'ProjectLeader',
                                   'ProjectManager',
                                   'ProjectMember',
                                   'RegistrationAgency',
                                   'RegistrationAuthority',
                                   'RelatedPerson',
                                   'Researcher',
                                   'ResearchGroup',
                                   'RightsHolder',
                                   'Sponsor',
                                   'Supervisor',
                                   'WorkPackageLeader',
                                   'Other',]}
                  },
                  'is_contact_person': {'type': 'boolean'},
                  'errors': ErrorSchema.schema,
              }}


class ContributorsSchema(JSONSchema):
    schema = {'type': 'array',
              'contains': {
                  'type': 'object',
                  'required': ['is_contact_person'],
                  'properties': {
                      'is_contact_person': {'type': 'boolean', 'enum': [True]},
                  },
              },
              'items': ContributorSchema.schema,
            }


class ContributorOutSchema(JSONSchema):
    schema = copy.deepcopy(ContributorSchema.schema)
    schema['properties']['contributor_role']['items']['enum'].remove('Creator')


class ContributorsOutSchema(JSONSchema):
    schema = copy.deepcopy(ContributorsSchema.schema)
    schema['items'] = ContributorOutSchema.schema


class CreatorSchema(JSONSchema):
    schema = copy.deepcopy(ContributorSchema.schema)
    schema['properties'].pop('contributor_role')
    schema['properties'].pop('is_contact_person')


class CreatorsSchema(JSONSchema):
    schema = {'type': 'array',
              'minItems': 1,
              'items': CreatorSchema.schema}


class DatasetDescriptionSchema(JSONSchema):
    schema = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['schema_version',  # missing should fail for this one ...
                     'name',
                     'description',
                     'funding',
                     'protocol_url_or_doi',
                     #'completeness_of_data_set'  # TODO versioned schema validation
                     'contributors',
                     'number_of_subjects',
                     'number_of_samples',],
        # TODO dependency to have title for complete if completeness is is not complete?
        'properties': {
            'errors': ErrorSchema.schema,
            'schema_version': {'type': 'string'},
            'name': {'type': 'string'},
            'description': {'type': 'string'},
            'keywords': {'type': 'array', 'items': {'type': 'string'}},
            'acknowledgements': {'type': 'string'},
            'funding': {'type': 'array',
                        'minItems': 1,
                        'items': {'type': 'string'}},
            'completeness_of_data_set': {'type': 'string'},
            'prior_batch_number': {'type': 'string'},
            'title_for_complete_data_set': {'type': 'string'},
            'originating_article_doi': {'type': 'array', 'items': {'type': 'string'}},  # TODO doi matching? maybe types?
            'number_of_subjects': {'type': 'integer'},
            'number_of_samples': {'type': 'integer'},
            'parent_dataset_id': {'type': 'string'},  # blackfynn id
            'protocol_url_or_doi': {
                'type': 'array',
                'minItems': 1,
                'items': {'type': 'string',
                          'pattern': simple_url_pattern}},
            'links': {
                'type': 'array',
                'minItems': 1,
                'items': {
                    'type': 'object',
                    'properties': {
                        'additional_links': {'type': 'string'},
                        'link_description': {'type': 'string'},  # FIXME only if there is an additional link? some using for protocol desc
                    }
                }
            },
            'examples': {
                'type': 'array',
                'minItems': 1,   # this pattern makes things self describing
                'items': {
                    'type': 'object',
                    'properties': {
                        'example_image_filename': {'type': 'string'},
                        'example_image_locator': {'type': 'string'},
                        'example_image_description': {'type': 'string'},
                    }
                }
            },
            'contributors': ContributorsSchema.schema,
        }
    }


class SubmissionSchema(JSONSchema):
    schema = {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'errors': ErrorSchema.schema,
            'submission': {
                'type': 'object',
                'required': ['sparc_award_number',
                             'milestone_achieved',
                             'milestone_completion_date'],
                # TODO allOf?
                'properties': {'sparc_award_number': {'type': 'string'},
                               'milestone_achieved': {'type': 'string'},
                               'milestone_completion_date': {'type': 'string'},}}}}


class UnitSchema(JSONSchema):
    schema = {'oneOf': [{'type': ['number', 'string']},
                        {'type': 'object',
                         # TODO enum on the allowed values here
                         'properties': {'type': {'type': 'string'},
                                        'value': {'type': 'number'},
                                        'unit': {'type': 'string'}}}]}


class SubjectsSchema(JSONSchema):
    schema = {
        'type': 'object',
        'required': ['subjects'],
        'properties': {'subjects': {'type': 'array',
                                    'minItems': 1,
                                    # FIXME there is currently no way to require that
                                    # a key be unique among all objects
                                    'items': {
                                        'type': 'object',
                                        'required': ['subject_id', 'species'],
                                        'properties': {
                                            'subject_id': {'type': 'string'},
                                            'species': {'type': 'string'},
                                            'strain': {'type': 'string'},  # TODO RRID
                                            'sex': {'type': 'string'},  # sex as a variable ?
                                            'mass': UnitSchema.schema,
                                            'body_mass': UnitSchema.schema,
                                            'weight': UnitSchema.schema,
                                            'body_weight': UnitSchema.schema,
                                            'age': UnitSchema.schema,
                                            'age_category': {'type': 'string'},  # TODO uberon
                                        },},},

                       'errors': ErrorSchema.schema,
                       'software': {'type': 'array',
                                    'minItems': 1,
                                    'items': {
                                        'type': 'object',
                                        'properties': {
                                            'software_version': {'type': 'string'},
                                            'software_vendor': {'type': 'string'},
                                            'software_url': {'type': 'string'},
                                            'software_rrid': {'type': 'string'},
                                        },},}}}

    # FIXME not implemented
    extras = [['unique', ['subjects', '*', 'subject_id']]]


class SamplesFileSchema(JSONSchema):
    schema = {
        'type': 'object',
        'required': ['samples'],
        'properties': {'samples': {'type': 'array',
                                    'minItems': 1,
                                    'items': {
                                        'type': 'object',
                                        'required': ['sample_id', 'subject_id'],
                                        'properties': {
                                            'sample_id': {'type': 'string'},
                                            'subject_id': {'type': 'string'},
                                            'group': {'type': 'string'},  # FIXME required ??

                                            'specimen': {'type': 'string'},  # what are we expecting here?
                                            'specimen_anatomical_location': {'type': 'string'},

                                            'species': {'type': 'string'},
                                            'strain': {'type': 'string'},  # TODO RRID
                                            'RRID_for_strain': {'type': 'string'},  # FIXME why is this in samples and not subjects?
                                            'genotype': {'type': 'string'},

                                            'sex': {'type': 'string'},  # sex as a variable ?

                                            'mass': UnitSchema.schema,
                                            'weight': UnitSchema.schema,

                                            'age': UnitSchema.schema,
                                            'age_category': {'type': 'string'},  # TODO uberon
                                            'age_range_min': UnitSchema.schema,
                                            'age_range_max': UnitSchema.schema,

                                            'handedness': {'type': 'string'},
                                            'disease': {'type': 'string'},
                                            'reference_atlas': {'type': 'string'},
                                            'protocol_title': {'type': 'string'},
                                            'protocol_url_or_doi': {'type': 'string'},  # protocols_io_location ??

                                            'experimental_log_file_name': {'type': 'string'},
                                        },},},

                       'errors': ErrorSchema.schema,
                       'software': {'type': 'array',
                                    'minItems': 1,
                                    'items': {
                                        'type': 'object',
                                        'properties': {
                                            'software_version': {'type': 'string'},
                                            'software_vendor': {'type': 'string'},
                                            'software_url': {'type': 'string'},
                                            'software_rrid': {'type': 'string'},
                                        },},}}}

    # FIXME not implemented
    extras = [['unique', ['samples', '*', 'sample_id']]]


class MetaOutSchema(JSONSchema):
    __schema = copy.deepcopy(DatasetDescriptionSchema.schema)
    extra_required = ['award_number',
                      'principal_investigator',
                      'species',
                      'organ',
                      'modality',
                      'techniques',
                      'contributor_count',
                      'uri_human',  # from DatasetMetadat
                      'uri_api',  # from DatasetMetadat
                      'files',
                      'dirs',
                      'size',
                      'folder_name',  # from DatasetMetadat
                      'title',
                      'schema_version',
                      'number_of_subjects',
                      'number_of_samples',
                      'timestamp_created',
                      'timestamp_updated',
                      'doi',  # may be none FIXME inconsistent
                      #'subject_count',
                      #'sample_count',
    ]
    __schema['required'].remove('contributors')
    __schema['required'].remove('name')
    __schema['required'] += extra_required
    __schema['properties'].pop('contributors')
    __schema['properties'].pop('name')
    __schema['properties'].update({
        'errors': ErrorSchema.schema,
        'dirs': {'type': 'integer'},
        'doi': {'oneOf': [{'type': 'string',
                           'pattern': doi_pattern,},
                          {'type': 'null'},]},
        'files': {'type': 'integer'},
        'size': {'type': 'integer'},
        'folder_name': {'type': 'string'},
        'title': {'type': 'string'},
        'timestamp_created': {'type': 'string',
                              'pattern': iso8601pattern,},
        'timestamp_updated': {'type': 'string',
                              'pattern': iso8601pattern,},
        'uri_human': {'type': 'string',
                      'pattern': r'^https://app\.blackfynn\.io/N:organization:',  # FIXME proper regex
        },
        'uri_api': {'type': 'string',
                    'pattern': r'^https://api\.blackfynn\.io/(datasets|packages)/',  # FIXME proper regex
        },
        'award_number': {'type': 'string',
                         'pattern': '^OT|^U18',},
        'principal_investigator': {'type': 'string'},
        'protocol_url_or_doi': {'type': 'array',
                                'minItems': 1,
                                'items': {'type': 'string',
                                          'pattern': simple_url_pattern,}},
        'additional_links': {'type': 'array',
                             'minItems': 1,
                             'items': {'type': 'string'}},
        'species': {'type': 'string'},
        'schema_version': {'type': 'string'},
        'organ': {'type': 'string'},
        'modality': {'type': 'array',
                     'minItems': 1,
                     'items': {
                         'type': 'string',
                     },
        },
        'techniques': {'type': 'array',
                       'minItems': 1,
                       'items': {
                           'type': 'string',
                       },
        },

        # TODO $ref these w/ pointer?
        # in contributor etc?

        'subject_count': {'type': 'integer'},
        'sample_count': {'type': 'integer'},
        'contributor_count': {'type': 'integer'},})

    schema = {'allOf': [__schema,
                        {'anyOf': [
                            #{'required': ['number_of_subjects']},
                            #{'required': ['number_of_samples']},
                            {'required': ['subject_count']},  # FIXME extract subjects from samples ?
                            {'required': ['sample_count']}
                        ]}]}


class DatasetOutSchema(JSONSchema):
    """ Schema that adds the id field since investigators shouldn't need to know
        the id to check the integrity of their data. We need it because that is
        a key piece of information that we use to link everything together. """

    __schema = copy.deepcopy(DatasetStructureSchema.schema['allOf'][0])
    __schema['required'] = ['id',
                            'meta',
                            'contributors',
                            'prov']
    __schema['properties'] = {'id': {'type': 'string',  # ye old multiple meta/bf id issue
                                     'pattern': '^N:dataset:'},
                              'meta': MetaOutSchema.schema,
                              'prov': ProvSchema.schema,
                              'errors': ErrorSchema.schema,
                              'contributors': ContributorsOutSchema.schema,
                              'creators': CreatorsSchema.schema,
                              # FIXME subjects_file might make the name overlap clearer
                              'subjects': SubjectsSchema.schema['properties']['subjects'],  # FIXME SubjectsOutSchema
                              'samples': SamplesFileSchema.schema['properties']['samples'],
                              # 'identifier_metadata': {'type': 'array'},  # FIXME temporary
                              'resources': {'type':'array',
                                            'items': {'type': 'object'},},
                              'inputs': {'type': 'object',
                                         # TODO do we need errors at this level?
                                         'properties': {'dataset_description_file': DatasetDescriptionSchema.schema,
                                                        'submission_file': SubmissionSchema.schema,
                                                        'subjects_file': SubjectsSchema.schema,},},}

    # FIXME switch to make samples optional since subject_id will always be there even in samples?
    schema = {'allOf': [__schema,
                        {'anyOf': [
                            {'required': ['subjects']},  # FIXME extract subjects from samples ?
                            {'required': ['samples']}
                        ]}]}


class StatusSchema(JSONSchema):
    schema = {'type': 'object',
              'required': ['submission_index', 'curation_index', 'error_index',
                           'submission_errors', 'curation_errors'],
              'properties': {
                  'status_on_platform': {'type': 'string'},
                  'submission_index': {'type': 'integer', 'minimum': 0},
                  'curation_index': {'type': 'integer', 'minimum': 0},
                  'error_index': {'type': 'integer', 'minimum': 0},
                  'submission_errors':{'type':'array',  # allow these to be empty
                                       'items': {'type': 'object'},},
                  'curation_errors':{'type':'array',  # allow these to be empty
                                     'items': {'type': 'object'},},
              }}


class PostSchema(JSONSchema):
    """ This is used to validate only the status schema part that is added to the DatasetOutSchema """
    schema = {
        'type': 'object',
        'required': ['status'],
        'properties': {'status': StatusSchema.schema,}
    }


class SummarySchema(JSONSchema):
    # TODO add expected counts
    schema = {'type': 'object',
              'required': ['id', 'meta', 'prov'],
              'properties': {'id': {'type': 'string',
                                    'pattern': '^N:organization:'},
                             'meta': {'type': 'object',
                                      'required': ['folder_name', 'count', 'uri_api', 'uri_human'],
                                      'properties': {'folder_name': {'type': 'string'},
                                                     # FIXME common source for these with MetaOutSchema
                                                     'uri_human': {'type': 'string',
                                                                   'pattern': r'^https://app\.blackfynn\.io/N:organization:',
        },
                                                     'uri_api': {'type': 'string',
                                                                 'pattern': r'^https://api\.blackfynn\.io/organizations/N:organization:',
                                                     },
                                                     'count': {'type': 'integer'},},},
                             'prov': {'type': 'object'},
                             'datasets': {'type': 'array',
                                          'minItems': 1,
                                          'items': {'type': 'object'},  # DatasetOutSchema already validated
                             }}}


class HeaderSchema(JSONSchema):
    schema = {'type': 'array',
              'items': {'type': 'string'}}


# constraints on MIS classes
class MISSpecimenSchema(JSONSchema):
    schema = {
        'type': 'object',
        'properties': {
            'TEMP:hasAge': {},
            'TEMP:hasAgeCategory': {},  # hasDevelopmentalStage
            'TEMP:hasBiologicalSex': {},  # TEMP because sparc:hasPhenotypicSex
            'TEMP:hasWeight': {},
            'TEMP:hasGenus': {},
            'TEMP:hasSpecies': {},
            'TEMP:hasStrain': {},  # race/ethnicity?
            'TEMP:hasRRID': {},
            'TEMP:hasGeneticVariation': {},

            'sparc:hasBodyMassIndex': {} ,
            'sparc:hasClinicalInformation': {},
            'sparc:hasDiseaseInformation': {},
            'sparc:hasDrugInformation': {},
            'sparc:hasFamilyHistory': {},
            'sparc:hasGender': {},
            'TEMP:hasHandedness': {},
            'TEMP:hasRace': {},
            'TEMP:hasEthnicity': {},
        },
        'oneOf': [
            {'allOf': [
                {'allOf':
                 [{'required': [
                     'TEMP:hasSpecies',
                     'TEMP:hasBiologicalSex',
                     'TEMP:hasWeight',]},
                  {'oneOf': [{'required': ['TEMP:hasAge']},
                             {'required': ['TEMP:hasAgeCategory']}]}]},
                {'oneOf': [
                    {'properties': {'TEMP:hasSpecies': {'enum': ['NCBITaxon:9606']}},
                     'required': [
                         'sparc:hasBodyMassIndex',
                         'sparc:hasClinicalInformation',
                         'sparc:hasDiseaseInformation',
                         'sparc:hasDrugInformation',
                         'sparc:hasFamilyHistory',
                         #'sparc:hasGender',
                         #'TEMP:hasHandedness',
                         'TEMP:hasRace',
                         'TEMP:hasEthnicity',
                     ]},
                    {'allOf': [
                        {'not': {'properties': {'TEMP:hasSpecies': {'enum': ['NCBITaxon:9606']}}}},
                        {'required': [
                            #'TEMP:hasSupplier',
                            #'TEMP:hasStrain',  # RRID? race/ethnicity?
                            'TEMP:hasRRID',
                         ]},
                    ]},
                ]},
            ]},
        ],
    }


class MISDatasetSchema(JSONSchema):
    moves = [  # FIXME this goes somehwere else
        # from datasetoutschema -> mis
        [[''], ['']],
        [[''], ['']],
        [[''], ['']],
        [[''], ['']],
        [[''], ['']],
        [[''], ['']],
        [['resources'], ['']],
        [['meta', 'protocol_url_or_doi'], ['']],
        #[['meta'], ['sparc:Resource']],
        [['subjects'], ['sparc:Specimen']],
        [['contributors'], ['sparc:Researcher']],  # ah the role confusion
    ]
    schema = {'type': 'object',
              'required': ['curies', 'rdf:type'],
              'properties': {
                  'curies': {'type': 'object'},
                  'rdf:type': {'type': 'string',
                               'pattern': 'sparc:Resource'},
                  #'sparc:'
                  'sparc:Specimen': {'type': 'array',
                                     'items': MISSpecimenSchema.schema},
              }
              }
