# This file is part of Indico.
# Copyright (C) 2002 - 2015 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import posixpath
import time
from collections import OrderedDict
from decimal import Decimal
from uuid import uuid4

from flask import has_request_context, session, request
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.event import listens_for
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import mapper

from indico.core import signals
from indico.core.config import Config
from indico.core.db import db
from indico.core.db.sqlalchemy import PyIntEnum, UTCDateTime
from indico.core.db.sqlalchemy.util.queries import increment_and_get
from indico.core.storage import StoredFileMixin
from indico.modules.payment import event_settings as event_payment_settings
from indico.util.date_time import now_utc
from indico.util.i18n import L_
from indico.util.locators import locator_property
from indico.util.string import return_ascii, format_repr
from indico.util.struct.enum import TitledIntEnum


class RegistrationState(TitledIntEnum):
    __titles__ = [None, L_('Completed'), L_('Pending'), L_('Rejected'), L_('Withdrawn'), L_('Awaiting payment')]
    complete = 1
    pending = 2
    rejected = 3
    withdrawn = 4
    unpaid = 5


def _get_next_friendly_id(context):
    """Get the next friendly id for a registration."""
    from indico.modules.events import Event
    event_id = context.current_parameters['event_id']
    assert event_id is not None
    return increment_and_get(Event._last_friendly_registration_id, Event.id == event_id)


class Registration(db.Model):
    """Somebody's registration for an event through a registration form"""
    __tablename__ = 'registrations'
    __table_args__ = (db.CheckConstraint('email = lower(email)', 'lowercase_email'),
                      db.Index(None, 'friendly_id', 'event_id', unique=True),
                      db.Index(None, 'registration_form_id', 'user_id', unique=True,
                               postgresql_where=db.text('NOT is_deleted AND (state NOT IN (3, 4))')),
                      db.Index(None, 'registration_form_id', 'email', unique=True,
                               postgresql_where=db.text('NOT is_deleted AND (state NOT IN (3, 4))')),
                      db.ForeignKeyConstraint(['event_id', 'registration_form_id'],
                                              ['event_registration.forms.event_id', 'event_registration.forms.id']),
                      {'schema': 'event_registration'})

    #: The ID of the object
    id = db.Column(
        db.Integer,
        primary_key=True
    )
    #: The unguessable ID for the object
    uuid = db.Column(
        UUID,
        index=True,
        unique=True,
        nullable=False,
        default=lambda: unicode(uuid4())
    )
    #: The human-friendly ID for the object
    friendly_id = db.Column(
        db.Integer,
        nullable=False,
        default=_get_next_friendly_id
    )
    #: The ID of the event
    event_id = db.Column(
        db.Integer,
        index=True,
        nullable=False
    )
    #: The ID of the registration form
    registration_form_id = db.Column(
        db.Integer,
        db.ForeignKey('event_registration.forms.id'),
        index=True,
        nullable=False
    )
    #: The ID of the user who registered
    user_id = db.Column(
        db.Integer,
        db.ForeignKey('users.users.id'),
        index=True,
        nullable=True
    )
    #: The ID of the latest payment transaction associated with this registration
    transaction_id = db.Column(
        db.Integer,
        db.ForeignKey('events.payment_transactions.id'),
        index=True,
        unique=True,
        nullable=True
    )
    #: The state a registration is in
    state = db.Column(
        PyIntEnum(RegistrationState),
        nullable=False,
    )
    #: The base registration fee (that is not specific to form items)
    base_price = db.Column(
        db.Numeric(8, 2),  # max. 999999.99
        nullable=False,
        default=0
    )
    #: The price modifier applied to the final calculated price
    price_adjustment = db.Column(
        db.Numeric(8, 2),  # max. 999999.99
        nullable=False,
        default=0
    )
    #: The date/time when the registration was recorded
    submitted_dt = db.Column(
        UTCDateTime,
        nullable=False,
        default=now_utc,
    )
    #: The email of the registrant
    email = db.Column(
        db.String,
        nullable=False
    )
    #: The first name of the registrant
    first_name = db.Column(
        db.String,
        nullable=False
    )
    #: The last name of the registrant
    last_name = db.Column(
        db.String,
        nullable=False
    )

    # The user linked to this registration
    user = db.relationship(
        'User',
        lazy=True,
        backref=db.backref(
            'registrations',
            lazy='dynamic'
            # XXX: a delete-orphan cascade here would delete registrations when NULLing the user
        )
    )
    #: The latest payment transaction associated with this registration
    transaction = db.relationship(
        'PaymentTransaction',
        lazy=True,
        foreign_keys=[transaction_id],
        post_update=True
    )
    #: The registration this data is associated with
    data = db.relationship(
        'RegistrationData',
        lazy=True,
        cascade='all, delete-orphan',
        backref=db.backref(
            'registration',
            lazy=True
        )
    )
    #: If the registration has been deleted
    is_deleted = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    #: The unique token used in tickets
    ticket_uuid = db.Column(
        UUID,
        index=True,
        unique=True,
        nullable=False,
        default=lambda: unicode(uuid4())
    )
    #: Whether the person has checked in
    checked_in = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    #: The date/time when the person has checked in
    checked_in_dt = db.Column(
        UTCDateTime,
        nullable=True
    )

    # relationship backrefs:
    # - registration_form (RegistrationForm.registrations)
    # - transactions (PaymentTransaction.registration)
    # - legacy_mapping (LegacyRegistrationMapping.registration)

    @classmethod
    def get_all_for_event(cls, event):
        """Retrieve all registrations in all registration forms of an event."""
        from indico.modules.events.registration.models.forms import RegistrationForm
        return Registration.find_all(Registration.is_active, ~RegistrationForm.is_deleted,
                                     RegistrationForm.event_id == event.id, _join=Registration.registration_form)

    @hybrid_property
    def is_active(self):
        return not self.is_cancelled and not self.is_deleted

    @is_active.expression
    def is_active(cls):
        return ~cls.is_cancelled & ~cls.is_deleted

    @hybrid_property
    def is_cancelled(self):
        return self.state in (RegistrationState.rejected, RegistrationState.withdrawn)

    @is_cancelled.expression
    def is_cancelled(self):
        return self.state.in_((RegistrationState.rejected, RegistrationState.withdrawn))

    @locator_property
    def locator(self):
        return dict(self.registration_form.locator, registration_id=self.id)

    @locator.registrant
    def locator(self):
        """A locator suitable for 'display' pages.

        It includes the UUID of the registration unless the current
        request doesn't contain the uuid and the registration is tied
        to the currently logged-in user.
        """
        loc = self.registration_form.locator
        if (not self.user or not has_request_context() or self.user != session.user or
                request.args.get('token') == self.uuid):
            loc['token'] = self.uuid
        return loc

    @locator.uuid
    def locator(self):
        """A locator that uses uuid instead of id"""
        return dict(self.registration_form.locator, token=self.uuid)

    @property
    def can_be_modified(self):
        regform = self.registration_form
        return regform.is_modification_open and regform.is_modification_allowed(self)

    @property
    def data_by_field(self):
        return {x.field_data.field_id: x for x in self.data}

    @property
    def billable_data(self):
        return [data for data in self.data if data.price]

    @property
    def full_name(self):
        return '{} {}'.format(self.first_name, self.last_name)

    @property
    def price(self):
        """The total price of the registration.

        This includes the base price, the field-specific price, and
        the custom price adjustment for the registrant.

        :rtype: Decimal
        """
        # we convert the calculated price (float) to a string to avoid this:
        # >>> Decimal(100.1)
        # Decimal('100.099999999999994315658113919198513031005859375')
        # >>> Decimal('100.1')
        # Decimal('100.1')
        calc_price = Decimal(str(sum(data.price for data in self.data)))
        base_price = self.base_price or Decimal('0')
        price_adjustment = self.price_adjustment or Decimal('0')
        return (base_price + price_adjustment + calc_price).max(0)

    @property
    def summary_data(self):
        """Export registration data nested in sections and fields"""

        def _fill_from_regform():
            for section in self.registration_form.sections:
                if not section.is_visible:
                    continue
                summary[section] = OrderedDict()
                for field in section.fields:
                    if not field.is_visible:
                        continue
                    summary[section][field] = field_summary[field]

        def _fill_from_registration():
            for field, data in field_summary.iteritems():
                section = field.parent
                summary.setdefault(section, OrderedDict())
                if field not in summary[section]:
                    summary[section][field] = data

        summary = OrderedDict()
        field_summary = {x.field_data.field: x for x in self.data}
        _fill_from_regform()
        _fill_from_registration()
        return summary

    @return_ascii
    def __repr__(self):
        return format_repr(self, 'id', 'registration_form_id', 'email', 'state',
                           user_id=None, is_deleted=False, _text=self.full_name)

    def get_personal_data(self):
        personal_data = {}
        for data in self.data:
            field = data.field_data.field
            if field.personal_data_type is not None and data.data:
                personal_data[field.personal_data_type.name] = data.friendly_data
        return personal_data

    def render_price(self):
        currency = event_payment_settings.get(self.registration_form.event, 'currency')
        return '{} {}'.format(self.price, currency)

    def update_state(self, approved=None, paid=None, rejected=None):
        """Update the state of the registration for a given action

        The accepted kwargs are the possible actions. ``True`` means that the
        action occured and ``False`` that it was reverted.
        """
        if sum(action is not None for action in (approved, paid, rejected)) > 1:
            raise Exception("More than one action specified")
        initial_state = self.state
        regform = self.registration_form
        invitation = self.invitation
        moderation_required = regform.moderation_enabled and (not invitation or not invitation.skip_moderation)
        with db.session.no_autoflush:
            payment_required = regform.event.has_feature('payment') and self.price
        if self.state is None:
            if moderation_required:
                self.state = RegistrationState.pending
            elif payment_required:
                self.state = RegistrationState.unpaid
            else:
                self.state = RegistrationState.complete
        elif self.state == RegistrationState.pending:
            if approved and payment_required:
                self.state = RegistrationState.unpaid
            elif approved:
                self.state = RegistrationState.complete
            elif rejected:
                self.state = RegistrationState.rejected
        elif self.state == RegistrationState.unpaid:
            if paid:
                self.state = RegistrationState.complete
            elif approved is False:
                self.state = Registration.pending
        elif self.state == RegistrationState.complete:
            if approved is False and payment_required is False and moderation_required:
                self.state = RegistrationState.pending
            elif paid is False and payment_required:
                self.state = RegistrationState.unpaid
        if self.state != initial_state:
            signals.event.registration_state_updated.send(self, previous_state=initial_state)


class RegistrationData(StoredFileMixin, db.Model):
    """Data entry within a registration for a field in a registration form"""

    __tablename__ = 'registration_data'
    __table_args__ = {'schema': 'event_registration'}

    # StoredFileMixin settings
    add_file_date_column = False
    file_required = False

    #: The ID of the registration
    registration_id = db.Column(
        db.Integer,
        db.ForeignKey('event_registration.registrations.id'),
        primary_key=True,
        autoincrement=False
    )
    #: The ID of the field data
    field_data_id = db.Column(
        db.Integer,
        db.ForeignKey('event_registration.form_field_data.id'),
        primary_key=True,
        autoincrement=False
    )
    #: The submitted data for the field
    data = db.Column(
        JSONB,
        nullable=False
    )

    #: The associated field data object
    field_data = db.relationship(
        'RegistrationFormFieldData',
        lazy=True,
        backref=db.backref(
            'registration_data',
            lazy=True,
            cascade='all, delete-orphan'
        )
    )

    # relationship backrefs:
    # - registration (Registration.data)

    @locator_property
    def locator(self):
        # a normal locator doesn't make much sense
        raise NotImplementedError

    @locator.file
    def locator(self):
        """A locator that pointsto the associated file."""
        if not self.filename:
            raise Exception('The file locator is only available if there is a file.')
        return dict(self.registration.locator, field_data_id=self.field_data_id, filename=self.filename)

    @property
    def friendly_data(self):
        return self.field_data.field.get_friendly_data(self)

    @property
    def price(self):
        return self.field_data.field.calculate_price(self)

    @property
    def summary_data(self):
        return {'data': self.friendly_data, 'price': self.price}

    @property
    def rdata(self):
        if self.field_data.field.input_type == 'file':
            return self.filename
        return self.data

    def _set_file(self, file_info):
        self.filename = file_info.get('name')
        self.content_type = file_info.get('content_type')
        # in case we are replacing a file
        self.storage_backend = None
        self.storage_file_id = None
        self.size = None
        if file_info:
            self.save(file_info['data'])

    file = property(fset=_set_file)
    del _set_file

    @return_ascii
    def __repr__(self):
        return '<RegistrationData({}, {}): {}>'.format(self.registration_id, self.field_data_id, self.data)

    def _build_storage_path(self):
        self.registration.registration_form.assign_id()
        self.registration.assign_id()
        path_segments = ['event', unicode(self.registration.event_id), 'registrations',
                         unicode(self.registration.registration_form.id), unicode(self.registration.id)]
        assert None not in path_segments
        # add timestamp in case someone uploads the same file again
        filename = '{}-{}-{}'.format(self.field_data.field_id, int(time.time()), self.filename)
        path = posixpath.join(*(path_segments + [filename]))
        return Config.getInstance().getAttachmentStorage(), path

    def render_price(self):
        currency = event_payment_settings.get(self.registration.registration_form.event, 'currency')
        return '{} {}'.format(self.price, currency)


@listens_for(mapper, 'after_configured', once=True)
def _mapper_configured():
    @listens_for(Registration.registration_form, 'set')
    def _set_event_id(target, value, *unused):
        target.event_id = value.event_id

    @listens_for(Registration.transaction, 'set')
    def _set_transaction_id(target, value, *unused):
        value.registration = target
