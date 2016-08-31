# -*- coding: utf8 -*-
import pytest

from ethereum import slogging

from raiden.mtree import check_proof
from raiden.tests.utils.messages import setup_messages_cb
from raiden.tests.utils.network import CHAIN
from raiden.tests.utils.transfer import (
    assert_synched_channels,
    channel,
    direct_transfer,
    get_received_transfer,
    get_sent_transfer,
    pending_mediated_transfer,
    register_secret,
)
from raiden.utils import sha3

# pylint: disable=too-many-locals,too-many-statements
slogging.configure(':DEBUG')


@pytest.mark.parametrize('privatekey_seed', ['settlement:{}'])
@pytest.mark.parametrize('number_of_nodes', [2])
def test_settlement(raiden_network, settle_timeout):
    app0, app1 = raiden_network  # pylint: disable=unbalanced-tuple-unpacking

    setup_messages_cb()

    asset_manager0 = app0.raiden.managers_by_asset_address.values()[0]
    asset_manager1 = app1.raiden.managers_by_asset_address.values()[0]

    chain0 = app0.raiden.chain

    channel0 = asset_manager0.partneraddress_channel[app1.raiden.address]
    channel1 = asset_manager1.partneraddress_channel[app0.raiden.address]

    balance0 = channel0.balance
    balance1 = channel1.balance

    amount = 10
    expiration = 5
    secret = 'secret'
    hashlock = sha3(secret)

    assert app1.raiden.address in asset_manager0.partneraddress_channel
    assert asset_manager0.asset_address == asset_manager1.asset_address
    assert channel0.external_state.netting_channel.address == channel1.external_state.netting_channel.address

    transfermessage = channel0.create_lockedtransfer(amount, expiration, hashlock)
    app0.raiden.sign(transfermessage)
    channel0.register_transfer(transfermessage)
    channel1.register_transfer(transfermessage)

    assert_synched_channels(
        channel0, balance0, [],
        channel1, balance1, [transfermessage.lock],
    )

    # Bob learns the secret, but Alice did not send a signed updated balance to
    # reflect this Bob wants to settle

    # get proof, that locked transfermessage was in merkle tree, with locked.root
    unlock_proof = channel1.our_state.balance_proof.get_proof_for(secret, hashlock)

    root = channel1.our_state.compute_merkleroot()

    assert check_proof(
        unlock_proof.merkle_proof,
        root,
        sha3(transfermessage.lock.as_bytes),
    )
    assert unlock_proof.lock_encoded == transfermessage.lock.as_bytes
    assert unlock_proof.secret == secret

    channel0.external_state.netting_channel.close(
        app0.raiden.address,
        transfermessage,
        None,
    )

    channel0.external_state.netting_channel.unlock(
        app0.raiden.address,
        [unlock_proof],
    )

    for _ in range(settle_timeout):
        chain0.next_block()

    channel0.external_state.netting_channel.settle()


@pytest.mark.parametrize('privatekey_seed', ['settled_lock:{}'])
@pytest.mark.parametrize('number_of_nodes', [4])
@pytest.mark.parametrize('channels_per_node', [CHAIN])
def test_settled_lock(assets_addresses, raiden_network, settle_timeout):
    """ Any transfer following a secret revealed must update the locksroot, so
    that an attacker cannot reuse a secret to double claim a lock.
    """
    asset = assets_addresses[0]
    amount = 30

    app0, app1, app2, app3 = raiden_network  # pylint: disable=unbalanced-tuple-unpacking

    # mediated transfer
    secret = pending_mediated_transfer(raiden_network, asset, amount)
    hashlock = sha3(secret)

    # get a proof for the pending transfer
    back_channel = channel(app1, app0, asset)
    secret_transfer = get_received_transfer(back_channel, 0)
    merkle_proof = back_channel.our_state.balance_proof.get_proof_for(secret, hashlock)

    # reveal the secret
    register_secret(raiden_network, asset, secret)

    # a new transfer to update the hashlock
    direct_transfer(app0, app1, asset, amount)

    forward_channel = channel(app0, app1, asset)
    last_transfer = get_sent_transfer(forward_channel, 1)

    # call close giving the secret for a transfer that has being revealed
    back_channel.external_state.netting_channel.close(
        app1.raiden.address,
        last_transfer,
        None
    )

    # check that the double unlock will failed
    with pytest.raises(Exception):
        back_channel.external_state.netting_channel.unlock(
            app1.raiden.address,
            [(merkle_proof, secret_transfer.lock.as_bytes, secret)],
        )

    # forward the block number to allow settle
    for _ in range(settle_timeout):
        app2.raiden.chain.next_block()

    back_channel.external_state.netting_channel.settle()

    participant0 = back_channel.external_state.netting_channel.contract.participants[app0.raiden.address]
    participant1 = back_channel.external_state.netting_channel.contract.participants[app1.raiden.address]

    assert participant0.netted == participant0.deposit - amount * 2
    assert participant1.netted == participant1.deposit + amount * 2


@pytest.mark.xfail()
@pytest.mark.parametrize('privatekey_seed', ['start_end_attack:{}'])
@pytest.mark.parametrize('number_of_nodes', [3])
def test_start_end_attack(asset_address, raiden_chain, deposit):
    """ An attacker can try to steal assets from a hub or the last node in a
    path.

    The attacker needs to use two addresses (A1 and A2) and connect both to the
    hub H, once connected a mediated transfer is initialized from A1 to A2
    through H, once the node A2 receives the mediated transfer the attacker
    uses the it's know secret and reveal to close and settles the channel H-A2,
    without revealing the secret to H's raiden node.

    The intention is to make the hub transfer the asset but for him to be
    unable to require the asset A1.
    """
    amount = 30

    asset = asset_address[0]
    app0, app1, app2 = raiden_chain  # pylint: disable=unbalanced-tuple-unpacking

    # The attacker creates a mediated transfer from it's account A1, to it's
    # account A2, throught the hub H
    secret = pending_mediated_transfer(raiden_chain, asset, amount)

    attack_channel = channel(app2, app1, asset)
    attack_transfer = get_received_transfer(attack_channel, 0)
    attack_contract = attack_channel.external_state.netting_channel.address
    hub_contract = channel(app1, app0, asset).external_state.netting_channel.address

    # the attacker can create a merkle proof of the locked transfer
    merkle_proof = attack_channel.our_state.locked.get_proof(attack_transfer)

    # start the settle counter
    attack_channel.netting_channel.close(
        app2.raiden.address,
        attack_transfer,
        None
    )

    # wait until the last block to reveal the secret
    for _ in range(attack_transfer.lock.expiration - 1):
        app2.raiden.chain.next_block()

    # since the attacker knows the secret he can net the lock
    attack_channel.netting_channel.unlock(
        [(merkle_proof, attack_transfer.lock, secret)],
    )
    # XXX: verify that the secret was publicized

    # at this point the hub might not know yet the secret, and won't be able to
    # claim the asset from the channel A1 - H

    # the attacker settle the contract
    app2.raiden.chain.next_block()

    attack_channel.netting_channel.settle(asset, attack_contract)

    # at this point the attack has the "stolen" funds
    attack_contract = app2.raiden.chain.asset_hashchannel[asset][attack_contract]
    assert attack_contract.participants[app2.raiden.address]['netted'] == deposit + amount
    assert attack_contract.participants[app1.raiden.address]['netted'] == deposit - amount

    # and the hub's channel A1-H doesn't
    hub_contract = app1.raiden.chain.asset_hashchannel[asset][hub_contract]
    assert hub_contract.participants[app0.raiden.address]['netted'] == deposit
    assert hub_contract.participants[app1.raiden.address]['netted'] == deposit

    # to mitigate the attack the Hub _needs_ to use a lower expiration for the
    # locked transfer between H-A2 than A1-H, since for A2 to acquire the asset
    # it needs to make the secret public in the block chain we publish the
    # secret through an event and the Hub will be able to require it's funds
    app1.raiden.chain.next_block()

    # XXX: verify that the Hub has found the secret, close and settle the channel

    # the hub has acquired it's asset
    hub_contract = app1.raiden.chain.asset_hashchannel[asset][hub_contract]
    assert hub_contract.participants[app0.raiden.address]['netted'] == deposit + amount
    assert hub_contract.participants[app1.raiden.address]['netted'] == deposit - amount
