# -*- coding: utf8 -*-
from __future__ import division

import pytest
from ethereum import slogging

from raiden.messages import DirectTransfer
from raiden.tests.utils.transfer import assert_synched_channels, channel
from raiden.utils import sha3

log = slogging.getLogger(__name__)  # pylint: disable=invalid-name
slogging.configure(':debug')


@pytest.mark.parametrize('privatekey_seed', ['setup:{}'])
@pytest.mark.parametrize('number_of_nodes', [2])
def test_setup(raiden_network, deposit, assets_addresses):
    app0, app1 = raiden_network  # pylint: disable=unbalanced-tuple-unpacking

    assets0 = app0.raiden.managers_by_asset_address.keys()
    assets1 = app1.raiden.managers_by_asset_address.keys()

    assert len(assets0) == 1
    assert len(assets1) == 1
    assert assets0 == assets1
    assert assets0[0] == assets_addresses[0]

    asset_address = assets0[0]
    channel0 = channel(app0, app1, asset_address)
    channel1 = channel(app1, app0, asset_address)

    assert channel0 and channel1

    assert_synched_channels(
        channel0, deposit, [],
        channel1, deposit, [],
    )


@pytest.mark.parametrize('privatekey_seed', ['transfer:{}'])
@pytest.mark.parametrize('number_of_nodes', [2])
def test_transfer(raiden_network, assets_addresses):
    app0, app1 = raiden_network  # pylint: disable=unbalanced-tuple-unpacking

    channel0 = channel(app0, app1, assets_addresses[0])
    channel1 = channel(app1, app0, assets_addresses[0])

    contract_balance0 = channel0.contract_balance
    contract_balance1 = channel1.contract_balance

    # check agreement on addresses
    address0 = channel0.our_state.address
    address1 = channel1.our_state.address
    assert channel0.asset_address == channel1.asset_address
    assert app0.raiden.managers_by_asset_address.keys()[0] == app1.raiden.managers_by_asset_address.keys()[0]
    assert app0.raiden.managers_by_asset_address.values()[0].partneraddress_channel.keys()[0] == app1.raiden.address
    assert app1.raiden.managers_by_asset_address.values()[0].partneraddress_channel.keys()[0] == app0.raiden.address

    netting_address = channel0.external_state.netting_channel.address
    netting_channel = app0.raiden.chain.netting_channel(netting_address)

    # check balances of channel and contract are equal
    details0 = netting_channel.detail(address0)
    details1 = netting_channel.detail(address1)

    assert contract_balance0 == details0['our_balance']
    assert contract_balance1 == details1['our_balance']

    assert_synched_channels(
        channel0, contract_balance0, [],
        channel1, contract_balance1, [],
    )

    amount = 10

    direct_transfer = channel0.create_directtransfer(amount=amount)
    app0.raiden.sign(direct_transfer)
    channel0.register_transfer(direct_transfer)
    channel1.register_transfer(direct_transfer)

    # check the contract is intact
    assert details0 == netting_channel.detail(address0)
    assert details1 == netting_channel.detail(address1)

    assert channel0.contract_balance == contract_balance0
    assert channel1.contract_balance == contract_balance1

    assert_synched_channels(
        channel0, contract_balance0 - amount, [],
        channel1, contract_balance1 + amount, [],
    )


@pytest.mark.parametrize('privatekey_seed', ['locked_transfer:{}'])
@pytest.mark.parametrize('number_of_nodes', [2])
def test_locked_transfer(raiden_network):
    app0, app1 = raiden_network  # pylint: disable=unbalanced-tuple-unpacking

    channel0 = app0.raiden.managers_by_asset_address.values()[0].partneraddress_channel.values()[0]
    channel1 = app1.raiden.managers_by_asset_address.values()[0].partneraddress_channel.values()[0]

    balance0 = channel0.balance
    balance1 = channel1.balance

    amount = 10

    # reveal_timeout <= expiration < contract.lock_time
    expiration = app0.raiden.chain.block_number() + 5

    secret = 'secret'
    hashlock = sha3(secret)

    locked_transfer = channel0.create_lockedtransfer(
        amount=amount,
        expiration=expiration,
        hashlock=hashlock,
    )
    app0.raiden.sign(locked_transfer)
    channel0.register_transfer(locked_transfer)
    channel1.register_transfer(locked_transfer)

    # don't update balances but update the locked/distributable/outstanding
    # values
    assert_synched_channels(
        channel0, balance0, [],
        channel1, balance1, [locked_transfer.lock],
    )

    channel0.register_secret(secret)
    channel1.register_secret(secret)

    # upon revelation of the secret both balances are updated
    assert_synched_channels(
        channel0, balance0 - amount, [],
        channel1, balance1 + amount, [],
    )


@pytest.mark.parametrize('privatekey_seed', ['interwoven_transfers:{}'])
@pytest.mark.parametrize('deposit', [2 ** 30])
@pytest.mark.parametrize('number_of_nodes', [2])
@pytest.mark.parametrize('number_of_transfers', [100])
def test_interwoven_transfers(number_of_transfers, raiden_network):  # pylint: disable=too-many-locals
    """ Can keep doing transaction even if not all secrets have been released. """
    def log_state():
        unclaimed = [
            transfer.lock.amount
            for pos, transfer in enumerate(transfers_list)
            if not transfers_claimed[pos]
        ]

        claimed = [
            transfer.lock.amount
            for pos, transfer in enumerate(transfers_list)
            if transfers_claimed[pos]
        ]
        log.info(
            'interwoven',
            claimed_amount=claimed_amount,
            distributed_amount=distributed_amount,
            claimed=claimed,
            unclaimed=unclaimed,
        )

    app0, app1 = raiden_network  # pylint: disable=unbalanced-tuple-unpacking

    channel0 = app0.raiden.managers_by_asset_address.values()[0].partneraddress_channel.values()[0]
    channel1 = app1.raiden.managers_by_asset_address.values()[0].partneraddress_channel.values()[0]

    contract_balance0 = channel0.contract_balance
    contract_balance1 = channel1.contract_balance

    expiration = app0.raiden.chain.block_number() + 5

    unclaimed_locks = []
    transfers_list = []
    transfers_claimed = []

    # start at 1 because we can't use amount=0
    transfers_amount = [i for i in range(1, number_of_transfers + 1)]
    transfers_secret = [str(i) for i in range(number_of_transfers)]

    claimed_amount = 0
    distributed_amount = 0

    for i, (amount, secret) in enumerate(zip(transfers_amount, transfers_secret)):
        locked_transfer = channel0.create_lockedtransfer(
            amount=amount,
            expiration=expiration,
            hashlock=sha3(secret),
        )

        # synchronized registration
        app0.raiden.sign(locked_transfer)
        channel0.register_transfer(locked_transfer)
        channel1.register_transfer(locked_transfer)

        # update test state
        distributed_amount += amount
        transfers_claimed.append(False)
        transfers_list.append(locked_transfer)
        unclaimed_locks.append(locked_transfer.lock)

        log_state()

        # test the synchronization and values
        assert_synched_channels(
            channel0, contract_balance0 - claimed_amount, [],
            channel1, contract_balance1 + claimed_amount, unclaimed_locks,
        )
        assert channel0.distributable == contract_balance0 - distributed_amount

        # claim a transaction at every other iteration, leaving the current one
        # in place
        if i > 0 and i % 2 == 0:
            transfer = transfers_list[i - 1]
            secret = transfers_secret[i - 1]

            # synchronized clamining
            channel0.register_secret(secret)
            channel1.register_secret(secret)

            # update test state
            claimed_amount += transfer.lock.amount
            transfers_claimed[i - 1] = True
            unclaimed_locks = [
                unclaimed_transfer.lock
                for pos, unclaimed_transfer in enumerate(transfers_list)
                if not transfers_claimed[pos]
            ]

            log_state()

            # test the state of the channels after the claim
            assert_synched_channels(
                channel0, contract_balance0 - claimed_amount, [],
                channel1, contract_balance1 + claimed_amount, unclaimed_locks,
            )
            assert channel0.distributable == contract_balance0 - distributed_amount


@pytest.mark.parametrize('privatekey_seed', ['register_invalid_transfer:{}'])
@pytest.mark.parametrize('number_of_nodes', [2])
def test_register_invalid_transfer(raiden_network):
    """ Regression test for registration of invalid transfer.

    The bug occurred if a transfer with an invalid allowance but a valid secret
    was registered, when the local end registered the transfer it would
    "unlock" the partners asset, but the transfer wouldn't be sent because the
    allowance check failed, leaving the channel in an inconsistent state.
    """
    app0, app1 = raiden_network  # pylint: disable=unbalanced-tuple-unpacking

    channel0 = app0.raiden.managers_by_asset_address.values()[0].partneraddress_channel.values()[0]
    channel1 = app1.raiden.managers_by_asset_address.values()[0].partneraddress_channel.values()[0]

    balance0 = channel0.balance
    balance1 = channel1.balance

    amount = 10
    expiration = app0.raiden.chain.block_number() + 5

    secret = 'secret'
    hashlock = sha3(secret)

    transfer1 = channel0.create_lockedtransfer(
        amount=amount,
        expiration=expiration,
        hashlock=hashlock,
    )

    # register a locked transfer
    app0.raiden.sign(transfer1)
    channel0.register_transfer(transfer1)
    channel1.register_transfer(transfer1)

    # assert the locked transfer is registered
    assert_synched_channels(
        channel0, balance0, [],
        channel1, balance1, [transfer1.lock],
    )

    # handcrafted transfer because channel.create_transfer won't create it
    transfer2 = DirectTransfer(
        nonce=channel0.our_state.nonce,
        asset=channel0.asset_address,
        transfered_amount=channel1.balance + balance0 + amount,
        recipient=channel0.partner_state.address,
        locksroot=channel0.partner_state.compute_merkleroot(),
        secret=secret,
    )
    app0.raiden.sign(transfer2)

    # this need to fail because the allowance is incorrect
    with pytest.raises(Exception):
        channel0.register_transfer(transfer2)

    with pytest.raises(Exception):
        channel1.register_transfer(transfer2)

    # the registration of a bad transfer need fail equaly on both channels
    assert_synched_channels(
        channel0, balance0, [],
        channel1, balance1, [transfer1.lock],
    )
