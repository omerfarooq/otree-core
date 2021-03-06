import copy
import django.test

from otree import constants_internal
import otree.common_internal
from otree.db import models


class GlobalSingleton(models.Model):
    """object that can hold site-wide settings. There should only be one
    GlobalSingleton object. Also used for wait page actions.
    """

    class Meta:
        app_label = "otree"

    default_session = models.ForeignKey('Session', null=True, blank=True)
    admin_access_code = models.RandomCharField(
        length=8, doc=('used for authentication to things only the '
                       'admin/experimenter should access')
    )


class ModelWithVars(models.Model):
    vars = models.JSONField(default=dict)

    class Meta:
        abstract = True

    def __init__(self, *args, **kwargs):
        super(ModelWithVars, self).__init__(*args, **kwargs)
        self._old_vars = copy.deepcopy(self.vars)

    def save(self, *args, **kwargs):
        # Trick otree_save_the_change to update vars
        if hasattr(self, '_changed_fields') and self.vars != self._old_vars:
            self._changed_fields['vars'] = self._old_vars
        super(ModelWithVars, self).save(*args, **kwargs)


# for now removing SaveTheChange
class Session(ModelWithVars):

    class Meta:
        # if i don't set this, it could be in an unpredictable order
        ordering = ['pk']
        app_label = "otree"

    config = models.JSONField(
        default=dict, null=True,
        doc=("the session config dict, as defined in the "
             "programmer's settings.py."))

    # label of this session instance
    label = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='For internal record-keeping')

    experimenter_name = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='For internal record-keeping')

    code = models.RandomCharField(
        length=8, doc="Randomly generated unique identifier for the session.")

    time_scheduled = models.DateTimeField(
        null=True, doc="The time at which the session is scheduled",
        help_text='For internal record-keeping', blank=True)

    time_started = models.DateTimeField(
        null=True,
        doc="The time at which the experimenter started the session")

    mturk_HITId = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='Hit id for this session on MTurk')
    mturk_HITGroupId = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='Hit id for this session on MTurk')
    mturk_qualification_type_id = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='Qualification type that is '
                  'assigned to each worker taking hit')

    # since workers can drop out number of participants on server should be
    # greater than number of participants on mturk
    # value -1 indicates that this session it not intended to run on mturk
    mturk_num_participants = models.IntegerField(
        default=-1,
        help_text="Number of participants on MTurk")

    mturk_sandbox = models.BooleanField(
        default=True,
        help_text="Should this session be created in mturk sandbox?")

    archived = models.BooleanField(
        default=False,
        doc=("If set to True the session won't be visible on the "
             "main ViewList for sessions"))

    git_commit_timestamp = models.CharField(
        max_length=200, null=True,
        doc=(
            "Indicates the version of the code (as recorded by Git) that was "
            "used to run the session, so that the session can be replicated "
            "later.\n Search through the Git commit log to find a commit that "
            "was made at this time."))

    comment = models.TextField(blank=True)

    _ready_to_play = models.BooleanField(default=False)

    _anonymous_code = models.RandomCharField(length=10)

    special_category = models.CharField(
        max_length=20, null=True,
        doc="whether it's a test session, demo session, etc.")

    # whether someone already viewed this session's demo links
    demo_already_used = models.BooleanField(default=False)

    # indicates whether a session has been fully created (not only has the
    # model itself been created, but also the other models in the hierarchy)
    ready = models.BooleanField(default=False)

    _pre_create_id = models.CharField(max_length=300, null=True)

    def __unicode__(self):
        return self.code

    @property
    def participation_fee(self):
        '''This method is deprecated from public API,
        but still useful internally (like data export)'''
        return self.config['participation_fee']

    @property
    def real_world_currency_per_point(self):
        '''This method is deprecated from public API,
        but still useful internally (like data export)'''
        return self.config['real_world_currency_per_point']

    @property
    def session_type(self):
        '''2015-07-10: session_type is deprecated
        this shim method will be removed eventually'''
        return self.config

    def is_open(self):
        return GlobalSingleton.objects.get().default_session == self

    def is_for_mturk(self):
        return (not self.is_demo()) and (self.mturk_num_participants > 0)

    def is_demo(self):
        return (
            self.special_category ==
            constants_internal.session_special_category_demo
        )

    def subsession_names(self):
        names = []
        for subsession in self.get_subsessions():
            app_name = subsession._meta.app_config.name
            name = '{} {}'.format(
                otree.common_internal.app_name_format(app_name),
                subsession.name()
            )
            names.append(name)
        if names:
            return ', '.join(names)
        else:
            return '[empty sequence]'

    def get_subsessions(self):
        lst = []
        app_sequence = self.config['app_sequence']
        for app in app_sequence:
            models_module = otree.common_internal.get_models_module(app)
            subsessions = models_module.Subsession.objects.filter(
                session=self
            ).order_by('round_number')
            lst.extend(list(subsessions))
        return lst

    def delete(self, using=None):
        for subsession in self.get_subsessions():
            subsession.delete()
        super(Session, self).delete(using)

    def get_participants(self):
        return self.participant_set.all()

    def payments_ready(self):
        for participants in self.get_participants():
            if not participants.payoff_is_complete():
                return False
        return True
    payments_ready.boolean = True

    def _create_groups_and_initialize(self):
        # if ppg is None, then the arrival time doesn't matter because
        # everyone is assigned to one big group.
        # otherwise, even in single-player games, you would have to wait
        # for other players to arrive
        # the drawback of this approach is that id_in_group is
        # predetermined, rather than by arrival time.
        # alternative design:
        # instead of checking ppg, we could also check if the game
        # contains a wait page
        # another alternative:
        # allow players to start even if the rest of the group hasn't arrived
        # but this might break some assumptions such as len(grp.get_players())
        # also, what happens if you get to the next round before
        # another player has started the first? you can't clone the
        # previous round's groups
        for subsession in self.get_subsessions():
            cond = (
                self.config.get('group_by_arrival_time') and
                subsession._Constants.players_per_group is not None
            )
            if cond:
                if subsession.round_number == 1:
                    subsession._set_players_per_group_list()
                subsession._create_empty_groups()
            else:
                subsession._create_groups()
            subsession._initialize()
            subsession.save()
        self._ready_to_play = True
        # assert self is subsession.session
        self.save()

    def mturk_requester_url(self):
        if self.mturk_sandbox:
            requester_url = (
                "https://requestersandbox.mturk.com/mturk/manageHITs"
            )
        else:
            requester_url = "https://requester.mturk.com/mturk/manageHITs"
        return requester_url

    def mturk_worker_url(self):
        if self.mturk_sandbox:
            worker_url = (
                "https://workersandbox.mturk.com/mturk/preview?groupId={}"
            ).format(self.mturk_HITGroupId)
        else:
            worker_url = (
                "https://www.mturk.com/mturk/preview?groupId={}"
            ).format(self.mturk_HITGroupId)
        return worker_url

    def advance_last_place_participants(self):
        participants = self.get_participants()

        c = django.test.Client()

        # in case some participants haven't started
        some_participants_not_visited = False
        for p in participants:
            if not p.visited:
                some_participants_not_visited = True
                c.get(p._start_url(), follow=True)

        if some_participants_not_visited:
            # refresh from DB so that _current_form_page_url gets set
            participants = self.participant_set.all()

        last_place_page_index = min([p._index_in_pages for p in participants])
        last_place_participants = [
            p for p in participants
            if p._index_in_pages == last_place_page_index
        ]

        for p in last_place_participants:
            # what if current_form_page_url hasn't been set yet?
            resp = c.post(
                p._current_form_page_url,
                data={constants_internal.auto_submit: True}, follow=True
            )
            assert resp.status_code < 400

    def build_participant_to_player_lookups(self):
        subsession_app_names = self.config['app_sequence']

        num_pages_in_each_app = {}
        for app_name in subsession_app_names:
            views_module = otree.common_internal.get_views_module(app_name)

            num_pages = len(views_module.page_sequence)
            num_pages_in_each_app[app_name] = num_pages

        for participant in self.get_participants():
            participant.build_participant_to_player_lookups(
                num_pages_in_each_app
            )
