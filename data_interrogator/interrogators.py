from django.conf import settings
from django.contrib.auth.decorators import user_passes_test
from django.core import exceptions
from django.db.models import F, Count, Min, Max, Sum, Value, Avg, ExpressionWrapper, DurationField, FloatField, CharField
from django.db.models import functions as func
from django.http import JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render

from datetime import timedelta

from data_interrogator.db import GroupConcat, DateDiff, ForceDate, SumIf
from data_interrogator import exceptions as di_exceptions

from django.apps import apps

def get_base_model(app_label,model):
    return apps.get_model(app_label.lower(), model.lower())


def normalise_field(text):
    return text.strip().replace('(','::').replace(')','').replace(".","__")


def clean_filter(text):
    maps = [('<=','lte'),('<','lt'),('>=','gte'),('>','gt'),('<>','ne'),('=','')]
    for a,b in maps:
        candidate = text.split(a)
        if len(candidate) == 2:
            if a is "=":
                return candidate[0], b, candidate[1]
            return candidate[0], '__%s'%b, candidate[1]
    return text


# Because of the risk of data leakage from User, Revision and Version tables,
# If a django user hasn't explicitly set up excluded models,
# we will ban interrogators from inspecting the User table
# as well as Revision and Version (which provide audit tracking and are available in django-revision)

math_infix_symbols = {
    '-': lambda a,b: a-b,
    '+': lambda a,b: a+b,
    '/': lambda a,b: a/b,
    '*': lambda a,b: a*b,
}

from enum import Enum
class allowable(Enum):
    ALL_APPS = 1
    ALL_MODELS = 1
    ALL_FIELDS = 3

class Interrogator():
    available_aggregations = {
            "min":Min,
            "max":Max,
            "sum":Sum,
            'avg':Avg,
            "count":Count,
            "substr":func.Substr,
            "group":GroupConcat,
            "concat":func.Concat,
            "sumif":SumIf,
        }
    errors = []

    # this list of:
    #   ('app_label', 'model_name')
    #   At some point will be this: ('app_label',)
    report_models = allowable.ALL_MODELS

    # both of these are lists of either:
    #   ('app_label',)
    #   ('app_label', 'model_name')
    #   Not this yet: ('app_label', 'model_name', ['list of field names'])
    allowed = allowable.ALL_MODELS
    excluded = []

    def __init__(self, report_models=None, allowed=None, excluded=None):
        if report_models is not None:
            self.report_models = report_models
        if allowed is not None:
            self.allowed = allowed
        if excluded is not None:
            self.excluded = excluded

        # Clean up rules if they aren't lower cased.
        fixed_excluded = []
        for rule in self.excluded:
            if len(rule) == 1:
                rule = (rule[0].lower(),)
            if len(rule) == 2:
                rule = (rule[0].lower(), rule[1].lower())
            if len(rule) == 3:
                rule = (rule[0].lower(), rule[1].lower(), rule[2])
            fixed_excluded.append(rule)
        self.excluded = fixed_excluded

        if self.allowed != allowable.ALL_MODELS:
            self.allowed_apps = [
                i[0] for i in allowed
                if type(i) is str or type(i) is tuple and len(i) == 1
            ]

        if self.allowed != allowable.ALL_APPS:
            self.allowed_models = [
                i[:2] for i in allowed
                if type(i) is tuple and len(i) == 2
            ]
        else:
            self.allowed_models = allowable.ALL_MODELS

    def get_model_queryset(self):
        return self.base_model.objects.all()

    def process_annotation_concat(self,column):
        pass

    def process_annotation(self,column):
        pass

    def verify_column(self, column):
        model = self.base_model
        args = column.split('__')
        for a in args:
            model = [f for f in model._meta.get_fields() if f.name==a][0].related_model

    def normalise_math(self,expression):
        if not any(s in expression for s in math_infix_symbols.keys()):
            # we're aggregating some mathy things, these are tricky
            return F(normalise_field(expression))

        import re
        math_operator_re = '[\-\/\+\*]'

        a, b = [v.strip() for v in re.split(math_operator_re, expression, 1)]
        first_operator = re.findall(math_operator_re, expression)[0]

        if first_operator == "-" and a.endswith('date') and b.endswith('date'):
            expr = ExpressionWrapper(
                DateDiff(
                    ForceDate(F(a)),
                    ForceDate(F(b))
                ), output_field=DurationField()
            )
        else:
            expr = ExpressionWrapper(
                math_infix_symbols[first_operator](F(a),F(b)),
                output_field=FloatField()
            )
        return expr

    def get_field_by_name(self, model, field_name):
        return model._meta.get_field(field_name)

    def is_excluded_field(sekf, field_path, base_model=None):
        """
        Accepts dundered path from model
        """
        # checking_model = base_model or self.base_model
        return False

    def has_forbidden_join(self, column, base_model=None):
        checking_model = base_model or self.base_model
        forbidden = False
        joins = column.split('__')
        for i, relation in enumerate(joins):
            if checking_model:
                try:
                    attr = self.get_field_by_name(checking_model, relation)
                    if attr.related_model:
                        if self.is_excluded_model(attr.related_model):
                            # Despite the join/field being named differently, this column is forbidden!
                            return True
                    checking_model = attr.related_model
                except exceptions.FieldDoesNotExist:
                    pass
        return forbidden

    def get_base_annotations(self):
        return {}

    def get_annotation(self, column):
        agg, field = column.split('::', 1)
        if agg == 'sumif':
            try:
                field, cond = field.split(',', 1)
            except:
                raise di_exceptions.InvalidAnnotationError("SUMIF must have a condition")
            field = self.normalise_math(field)
            conditions = {}
            for condition in cond.split(','):
                condition_key, condition_val = condition.split('=', 1)
                conditions[normalise_field(condition_key)] = normalise_field(condition_val)
            annotation = self.available_aggregations[agg](field=field, **conditions)
        elif agg == 'join':
            fields = []
            for f in field.split(','):
                if f.startswith(('"', "'")):
                    # its a string!
                    fields.append(Value(f.strip('"').strip("'")))
                else:
                    fields.append(f)
            annotation = self.available_aggregations[agg](*fields)
        elif agg == "substr":
            field, i, j = (field.split(',') + [None])[0:3]
            annotation = self.available_aggregations[agg](field, i, j)
        else:
            field = self.normalise_math(field)
            annotation = self.available_aggregations[agg](field, distinct=False)
        return annotation
    def is_allowed_model(self, model):
        pass

    def is_excluded_model(self, model_class):
        app_label = model_class._meta.app_label
        model_name = model_class._meta.model_name

        # if self.allowed = allowable.ALL_MODELS
        return app_label in self.excluded or (app_label, model_name) in self.excluded

    def validate_report_model(self, base_model):
        app_label,model = base_model.split(':',1)
        base_model = apps.get_model(app_label.lower(), model.lower())

        extra_data = {}
        if self.report_models == allowable.ALL_MODELS:
            return base_model, extra_data

        for opts in self.report_models:
            if opts[:2] == (app_label, model):
                return base_model, extra_data

        self.base_model = None
        raise di_exceptions.ModelNotAllowedException()

    def generate_queryset(self, base_model, columns=[], filters=[], order_by=[], limit=None, offset=0):

        errors = []
        base_model_data = {}
        annotation_filters = {}
        output_columns = []
        count=0

        annotations = self.get_base_annotations()
        query_columns = []
        self.base_model, base_model_data = self.validate_report_model(base_model)
        wrap_sheets = base_model_data.get('wrap_sheets',{})

        expression_columns = []
        for column in columns:
            if column == "":
                continue # do nothings for empty fields
                
            var_name = None
            # TODO: This isn't working properly right now, but we can ignore it.
            if ':=' in column: # assigning a variable
                var_name,column = column.split(':=',1)
            # map names in UI to django functions
            column = normalise_field(column)
            
            if self.has_forbidden_join(column):
                errors.append("Joining tables with the column [{}] is forbidden, this column is removed from the output.".format(column))
                continue

            if '::' in column:
                check_col = column.split('::',1)[-1]
                if self.has_forbidden_join(check_col):
                    errors.append("Aggregating tables using the column [{}] is forbidden, this column is removed from the output.".format(column))
                    continue

            if var_name is None:
                var_name = column

            if column.startswith(tuple([a+'::' for a in self.available_aggregations.keys()])):
                annotations[var_name] = self.get_annotation(column)

            elif any(s in column for s in math_infix_symbols.keys()):
                annotations[var_name] = self.normalise_math(column)
                expression_columns.append(var_name)
            else:
                if column in wrap_sheets.keys():
                    cols = wrap_sheets.get(column).get('columns',[])
                    query_columns = query_columns + cols
                else:
                    if var_name == column:
                        query_columns.append(var_name)
                    else:
                        annotations[var_name] = F(column)
            output_columns.append(var_name)
    
        rows = self.get_model_queryset()
    
        _filters = {}
        excludes = {}
        filters_all = {}
        for i, expression in enumerate(filters):
            # cleaned = clean_filter(normalise_field(expression))
            field, exp, val = clean_filter(normalise_field(expression))
            if self.has_forbidden_join(field):
                errors.append("Filtering with the column [{}] is forbidden, this filter is removed from the output.".format(field))
                continue

            key = '%s%s'%(field.strip(),exp)
            val = val.strip()

            if val.startswith('~'):
                val = F(val[1:])
            elif key.endswith('date'): # in key:
                val = (val+'-01-01')[:10] # If we are filtering by a date, make sure its 'date-like'
            elif key.endswith('__isnull'):
                if val == 'False' or val == '0':
                    val = False
                else:
                    val = bool(val)

            if '::' in field:
                # we got an annotated filter
                agg,f = field.split('::',1)
                field = 'f%s%s'%(i,field)
                key = 'f%s%s'%(i,key)
                annotations[field] = self.available_aggregations[agg](f, distinct=True)
                annotation_filters[key] = val
            elif key in annotations.keys():
                annotation_filters[key] = val
            elif key.split('__')[0] in expression_columns:
                k = key.split('__')[0]
                if 'date' in k and key.endswith('date') or 'date' in str(annotations[k]):
                    val,period = (val.rsplit(' ',1) + ['days'])[0:2] # this line is complicated, just in case there is no period or space
                    period = period.rstrip('s') # remove plurals
                    
                    kwargs = {}
                    big_multipliers = {
                        'day':1,
                        'week':7,
                        'fortnight': 14, # really?
                        'month':30, # close enough
                        'year': 365,
                        'decade': 10*365, # wise guy huh?
                        }
                        
                    little_multipliers = {
                        'second':1,
                        'minute':60,
                        'hour':60*60,
                        'microfortnight': 1.2, # sure why not?
                        }
                        
                    if big_multipliers.get(period,None):
                        kwargs['days'] = int(val)*big_multipliers[period]
                    elif little_multipliers.get(period,None):
                        kwargs['seconds'] = int(val)*little_multipliers[period]
                        
                    annotation_filters[key] = timedelta(**kwargs)
                        
                else:
                    annotation_filters[key] = val
    
            elif key.endswith('__all'):
                key = key.rstrip('_all')
                val = [v for v in val.split(',')]
                filters_all[key] = val
            else:
                exclude = key.endswith('!')
                if exclude:
                    key = key[:-1]
                if key.endswith('__in'):
                    val = [v for v in val.split(',')]
                if exclude:
                    excludes[key] = val
                else:
                    _filters[key] = val

        rows = rows.filter(**_filters)
        for key,val in filters_all.items():
            for v in val:
                rows = rows.filter(**{key:v})
        rows = rows.exclude(**excludes)
        rows = rows.values(*query_columns)

        if annotations:
            rows = rows.annotate(**annotations)
            rows = rows.filter(**annotation_filters)
        if order_by:
            ordering = map(normalise_field,order_by)
            rows = rows.order_by(*ordering)

        if limit:
            lim = abs(int(limit))
            rows = rows[offset:lim]

        return rows, errors, output_columns, base_model_data

    def interrogate(self, base_model, columns=[], filters=[], order_by=[], limit=None, offset=0):
        errors = []
        base_model_data = {}
        output_columns = []
        count = 0
        rows = []

        try:
            rows, errors, output_columns, base_model_data = self.generate_queryset(
                base_model, columns, filters, order_by, limit, offset
            )
            if errors:
                rows = rows.none()
            rows = list(rows) # force a database hit to check the state of things
            count = len(rows)
        except di_exceptions.InvalidAnnotationError as e:
                errors.append(e)
        except ValueError as e:
            rows = []
            if limit is None:
                errors.append("Limit must be a number")
            elif limit < 1:
                errors.append("Limit must be a number greater than zero")
            else:
                errors.append("Something when wrong - %s"%e)
        except IndexError as e:
            rows = []
            errors.append("No rows returned for your query, try broadening your search.")
        except exceptions.FieldError as e:
            rows = []
            raise
            if str(e).startswith('Cannot resolve keyword'):
                field = str(e).split("'")[1]
                errors.append("The requested field '%s' was not found in the database."%field)
            else:
                errors.append("An error was found with your query:\n%s"%e)
        except Exception as e:
            rows = []
            raise
            errors.append("Something when wrong - %s"%e)
    
        return {
            'rows':rows, 'count':count, 'columns':output_columns, 'errors':errors,
            'base_model':base_model_data
        }

class PivotInterrogator(Interrogator):
    def __init__(self, aggregators, **kwargs):
        super().__init__(**kwargs)
        self.aggregators = aggregators

    def get_base_annotations(self):
        aggs = {
            x:self.get_annotation(normalise_field(x)) for x in self.aggregators
            if not self.has_forbidden_join(column=x)
        }
        aggs.update({"cell": Count(1)})
        return aggs

    def pivot(self):
        # only accept the first two valid columns
        self.columns = [
            normalise_field(c) for c in self.columns
            if not self.has_forbidden_join(column=c)
        ][:2]

        data = self.interrogate()
        out_rows = {}

        col_head = self.base_model.objects.values(self.columns[0]).order_by(self.columns[0]).distinct()

        x,y = self.columns[:2]

        from collections import OrderedDict
        default = OrderedDict([(c[x],{'count':0}) for c in col_head])
        for r in data['rows']:
            this_row = out_rows.get(r[y],default.copy())
            this_row[r[x]] = {  'count':r['cell'],
                                'aggs':[(k,v) for k,v in r.items() if k not in ['cell',x,y]]
                            }
            out_rows[r[y]] = this_row

        return {
            'rows':out_rows,'col_head':col_head,'errors':data['errors'],
            'base_model':data['base_model'],'headers':data['headers']
        }
