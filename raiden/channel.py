# -*- coding: utf8 -*-
from ethereum import slogging
from ethereum.utils import encode_hex

from raiden.messages import (
    BaseError,
    DirectTransfer,
    Lock,
    LockedTransfer,
    TransferTimeout,
)
from raiden.mtree import merkleroot, get_proof
from raiden.utils import sha3, pex, lpex

log = slogging.getLogger(__name__)  # pylint: disable=invalid-name


class InvalidNonce(BaseError):
    pass


class InvalidSecret(BaseError):
    pass


class InvalidLocksRoot(BaseError):
    pass


class InvalidLockTime(BaseError):
    pass


class InsufficientBalance(BaseError):
    pass


class LockedTransfers(object):
    """ Mapping container for transactions with locked asset, mapping lockroot
    to transfer.

    Container class used to keep track of transfers that have asset locked
    because of the lack of the secret. The container can produce a updated
    merkle root or proof that a given lock is included in the set.
    """

    def __init__(self):
        self.locked = dict()  #: A mapping from hashlock to the transfer
        self._cached_lock_hashes = []  #: lock's hash cache `sha3(amount || expiration || hashlock)`
        self._cached_root = None  #: the merkle proof of the current transfers

    def __contains__(self, hashlock):
        """ Return True if there is a pending transfer with the given hashlock, False otherwise. """
        return hashlock in self.locked

    def __len__(self):
        """ Return the count of pending transfers. """
        return len(self.locked)

    def __getitem__(self, hashlock):
        """ Lookup a pending transfer by it's hashlock. """
        return self.locked[hashlock]

    def __iter__(self):
        return self.locked.iterkeys()

    def add(self, transfer):
        """ Add a new mediated transfer into the set, registering it's lock.

        This is used when a new transfer with locked asset is being created and
        the path of nodes is being traversed from *initiator* to *target*. The
        sender is the node from the channel that will transfer a given amount
        of asset once the secret is revealed.

        Motivation
        ----------

        The sender needs to use this method before sending a locked transfer,
        otherwise the calculated locksroot of the transfer message will be
        invalid and the transfer will be rejected by the partner. Since the
        sender wants the transfer to be accepted by the receiver otherwise the
        transfer won't proceed and the sender won't receive its fee.

        The receiver needs to use this method to update the container with a
        _valid_ transfer, otherwise the locksroot will not contain the pending
        transfer. The receiver needs to ensure that the merkle root has the
        hashlock include, otherwise it won't be able to claim it.

        Args:
            transfer (LockedTransfer): The transfer to be added.
        """
        assert transfer.lock.hashlock not in self.locked
        self.locked[transfer.lock.hashlock] = transfer
        self._cached_lock_hashes.append(sha3(transfer.lock.as_bytes))
        self._cached_root = None

    get = __getitem__

    def remove(self, hashlock):
        """ Remove a transfer from the locked set.

        Args:
            hashlock: The hashlock of the corresponding transfer.
        """
        self._cached_lock_hashes.remove(sha3(self.get(hashlock).lock.as_bytes))
        self._cached_root = None
        del self.locked[hashlock]

    @property
    def outstanding(self):
        """ Return the amount of asset that is locked in this container. """
        return sum(
            transfer.lock.amount
            for transfer in self.locked.values()
        )

    # XXX: Remove expired transfers?

    @property
    def root(self):
        if self._cached_root is None:
            self._cached_root = merkleroot(self._cached_lock_hashes)
        return self._cached_root

    def root_with(self, lock=None, exclude=None):
        """ Calculate the merkle root of the hashes in the container.

        Args:
            lock: Additional hashlock to be included in the merkle tree, used
                to calculate the updated merkle root without changing the store.
            exclude: Hashlock to be ignored, used to calculated a the updated
                merkle root without changing the store.
        """
        if lock and not isinstance(lock, Lock):
            raise ValueError('lock must be a Lock')

        if exclude and not isinstance(exclude, Lock):
            raise ValueError('exclude must be a Lock')

        lock_hash = exclude_hash = None

        # temporarily add
        if lock:
            lock_hash = sha3(lock.as_bytes)
            self._cached_lock_hashes.append(lock_hash)

        # temporarily remove
        if exclude:
            exclude_hash = sha3(exclude.as_bytes)
            self._cached_lock_hashes.remove(exclude_hash)

        root = merkleroot(self._cached_lock_hashes)

        # remove the temporarily added hash
        if lock_hash:
            assert lock_hash in self._cached_lock_hashes
            self._cached_lock_hashes.remove(lock_hash)

        # reinclude the temporarily removed hash
        if exclude:
            self._cached_lock_hashes.append(exclude_hash)

        return root

    def get_proof(self, transfer):
        """ Return the merkle proof that transfer is one of the locked
        transfers in the container.
        """
        hashlock = transfer.lock.hashlock
        transfer = self.locked[hashlock]
        proof_for = sha3(transfer.lock.as_bytes)
        proof = get_proof(self._cached_lock_hashes, proof_for)
        return proof


class ChannelEndState(object):
    """ Tracks the state of one of the participants in a channel. """

    def __init__(self, participant_address, participant_balance):
        # since ethereum only uses integral values we cannot use float/Decimal
        if not isinstance(participant_balance, (int, long)):
            raise ValueError('participant_balance must be an integer.')

        self.contract_balance = participant_balance  #: total asset locked in the contract
        self.transfered_amount = 0  #: total amount of transfered asset
        self.address = participant_address  #: node's address

        # 0 is used in the netting contract to represent the lack of a
        # transfer, so this value must start at 1
        self.nonce = 1  #: sequential nonce, current value has not been used
        self.locked = LockedTransfers()  #: locked received

    def balance(self, other):
        """ Return the current available balance of the participant. """
        return self.contract_balance - self.transfered_amount + other.transfered_amount

    def update_contract_balance(self, contract_balance):
        """ Update the current participant's balance. """
        self.contract_balance = contract_balance

    def distributable(self, other):
        """ Return the available amount of the asset that can be transfered in
        the channel `(total - locked)`.
        """
        return self.balance(other) - other.locked.outstanding

    def claim_locked(self, partner, secret, locksroot=None):
        """ Update the balance of this end of the channel by claiming the
        transfer.

        This methods needs to be called once a `Secret` message is received,
        otherwise the nodes can get out-of-sync and messages will be rejected.

        Args:
            partner: The partner end from which we are receiving, this is
                required to keep both ends in sync.
            secret: Releases a lock.

        Raises:
            InvalidSecret: If there is no lock register for the given secret
                (or `hashlock` if given).

        Returns:
            float: The amount that was locked.
        """
        # XXX: The secret is being discarded right away, it needs to be saved
        # at least until the next partner's message with an updated balance and
        # locksroot that acknowledges the unlocked asset
        hashlock = sha3(secret)

        if hashlock not in self.locked:
            raise InvalidSecret(hashlock)

        # The balance and lockroot work hand-in-hand, both values need to be
        # synchronized at all times with the penalty of losing asset.
        #
        # This section works for cooperative multitasking, for preempted
        # multitasking synchronization needs to be done.

        # start of the critical write section
        lock = self.locked[hashlock].lock
        if locksroot and self.locked.root_with(None, exclude=lock) != locksroot:
            raise InvalidLocksRoot(hashlock)

        # Indirectly update balance by setting the partner's transfered_amount,
        # the new value needs to be checked
        amount = lock.amount
        partner.transfered_amount += amount

        # Important: as a sender remove the freed hashlock to avoid double
        # netting of a locked transfer (as a receiver this is "just" synching)
        self.locked.remove(hashlock)
        # end of the critical write section


class ChannelExternalState(object):
    def __init__(self, register_channel_for_hashlock, get_block_number, netting_channel):
        self.register_channel_for_hashlock = register_channel_for_hashlock
        self.get_block_number = get_block_number

        self.netting_channel = netting_channel
        self.opened_block = netting_channel.opened()
        self.closed_block = netting_channel.closed()
        self.settled_block = netting_channel.settled()

    def isopen(self):
        if self.closed_block != 0:
            return False

        if self.opened_block != 0:
            return True

        return False


class Channel(object):
    # pylint: disable=too-many-instance-attributes,too-many-arguments

    def __init__(self, our_state, partner_state, external_state,
                 asset_address, reveal_timeout, settle_timeout):

        self.our_state = our_state
        self.partner_state = partner_state

        self.asset_address = asset_address
        self.reveal_timeout = reveal_timeout
        self.settle_timeout = settle_timeout
        self.external_state = external_state

        self.received_transfers = []
        self.sent_transfers = []  #: transfers that were sent, required for settling
        self.transfer_callbacks = []  # list of (Transfer, callback) tuples

    @property
    def isopen(self):
        return self.external_state.isopen()

    @property
    def contract_balance(self):
        """ Return the amount of asset used to open the channel. """
        return self.our_state.contract_balance

    @property
    def transfered_amount(self):
        """ Return how much we transfered to partner. """
        return self.our_state.transfer_amount

    @property
    def balance(self):
        """ Return our current balance.

        Balance is equal to `initial_deposit + received_amount - sent_amount`,
        were both `receive_amount` and `sent_amount` are unlocked.
        """
        return self.our_state.balance(self.partner_state)

    @property
    def distributable(self):
        """ Return the available amount of the asset that our end of the
        channel can transfer to the partner.
        """
        return self.our_state.distributable(self.partner_state)

    @property
    def locked(self):
        """ Return the current amount of our asset that is locked waiting for a
        secret.

        The locked value is equal to locked transfers that have being
        initialized but the secret has not being revealed.
        """
        return self.partner_state.locked.outstanding

    @property
    def outstanding(self):
        """ Return the current amount of asset that is we are waiting a secret
        to be freed.
        """
        return self.our_state.locked.outstanding

    def handle_callbacks(self, transfer):
        # TODO: dict mapping transfer -> callback + cleanup
        for pos, (callback_transfer, callback) in enumerate(self.transfer_callbacks):
            if callback_transfer is transfer:
                callback(None, True)
                del self.transfer_callbacks[pos]

    def get_state_for(self, node_address_bin):
        if self.our_state.address == node_address_bin:
            return self.our_state

        if self.partner_state.address == node_address_bin:
            return self.partner_state

        raise Exception('Unknow address {}'.format(encode_hex(node_address_bin)))

    def claim_locked(self, secret, locksroot=None):
        """ Claim locked transfer from any of the ends of the channel.

        Args:
            secret: The secret that releases a locked transfer.
        """
        hashlock = sha3(secret)

        # receiving a secret (releasing our funds)
        if hashlock in self.our_state.locked:
            log.debug('ASSET UNLOCKED node:{} asset:{} hashlock:{} amount:{}'.format(
                pex(self.our_state.address),
                pex(self.asset_address),
                pex(hashlock),
                self.our_state.locked[hashlock].lock.amount,
            ))
            self.our_state.claim_locked(self.partner_state, secret, locksroot)

        # sending a secret (updating the mirror)
        elif hashlock in self.partner_state.locked:
            log.debug('ASSET UNLOCKED node:{} asset:{} hashlock:{} amount:{}'.format(
                pex(self.our_state.address),
                pex(self.asset_address),
                pex(hashlock),
                self.partner_state.locked[hashlock].lock.amount,
            ))
            self.partner_state.claim_locked(self.our_state, secret, locksroot)
        else:
            raise ValueError('The secret doesnt unlock any hashlock')

    def register_transfer(self, transfer, callback=None):
        """ Register a signed transfer, updating the channel's state accordingly. """

        if transfer.recipient == self.partner_state.address:
            self.register_transfer_from_to(
                transfer,
                from_state=self.our_state,
                to_state=self.partner_state,
            )

            self.sent_transfers.append(transfer)

            if callback:
                self.transfer_callbacks.append((transfer, callback))

        elif transfer.recipient == self.our_state.address:
            self.register_transfer_from_to(
                transfer,
                from_state=self.partner_state,
                to_state=self.our_state,
            )
            self.received_transfers.append(transfer)

        else:
            raise ValueError('Invalid address')

    def register_transfer_from_to(self, transfer, from_state, to_state):  # noqa pylint: disable=too-many-branches
        """ Validates and register a signed transfer, updating the channel's state accordingly.

        Note:
            The transfer must be register before it is sent, not on
            acknowledgement. That is necessary for to reasons:

            - Guarantee that the transfer is valid.
            - Avoiding sending a new transaction without funds.

        Raises:
            InsufficientBalance: If the transfer is negative or above the distributable amount.
            InvalidLocksRoot: If locksroot check fails.
            InvalidLockTime: If the transfer has expired.
            InvalidNonce: If the expected nonce does not match.
            InvalidSecret: If there is no lock registered for the given secret.
            ValueError: If there is an address mismatch (asset or node address).
        """
        if transfer.asset != self.asset_address:
            raise ValueError('Asset address mismatch')

        if transfer.recipient != to_state.address:
            raise ValueError('Unknow recipient')

        if transfer.sender != from_state.address:
            raise ValueError('Unsigned transfer')

        if transfer.transfered_amount < from_state.transfered_amount:
            raise ValueError('Negative transfer')

        # nonce is changed only when a transfer is un/registered, if the test
        # fail either we are out of sync, a message out of order, or it's an
        # forged transfer
        if transfer.nonce < 1 or transfer.nonce != from_state.nonce:
            raise InvalidNonce(transfer)

        amount = transfer.transfered_amount - from_state.transfered_amount
        distributable = from_state.distributable(to_state)

        if amount > distributable:
            raise InsufficientBalance(transfer)

        if isinstance(transfer, LockedTransfer):
            block_number = self.external_state.get_block_number()

            if amount + transfer.lock.amount > distributable:
                raise InsufficientBalance(transfer)

            # As a receiver: Check that all locked transfers are registered in
            # the locksroot, if any hashlock is missing there is no way to
            # claim it while the channel is closing
            expected_locksroot = to_state.locked.root_with(transfer.lock)
            if expected_locksroot != transfer.locksroot:
                log.error(
                    'LOCKSROOT MISMATCH node:{} {} > {}'.format(
                        pex(self.our_state.address),
                        pex(from_state.address),
                        pex(to_state.address),
                        pex(self.partner_state.address),
                    ),
                    expected_locksroot=pex(expected_locksroot),
                    received_locksroot=pex(transfer.locksroot),
                    current_locksroot=pex(to_state.locked.root),
                )
                raise InvalidLocksRoot(transfer)

            # As a receiver: If the lock expiration is larger than the settling
            # time a secret could be revealed after the channel is settled and
            # we won't be able to claim the asset
            if not transfer.lock.expiration - block_number < self.settle_timeout:
                log.error(
                    "Transfer expiration doesn't allow for corret settlement.",
                    lock_expiration=transfer.lock.expiration,
                    current_block=block_number,
                    settle_timeout=self.settle_timeout,
                )

                raise ValueError("Transfer expiration doesn't allow for corret settlement.")

            if not transfer.lock.expiration - block_number > self.reveal_timeout:
                log.error(
                    'Expiration smaller than the minimum requried.',
                    lock_expiration=transfer.lock.expiration,
                    current_block=block_number,
                    reveal_timeout=self.reveal_timeout,
                )

                raise ValueError('Expiration smaller than the minimum requried.')

        # all checks need to be done before the internal state of the channel
        # is changed, otherwise if a check fails and state was changed the
        # channel will be left trashed

        if isinstance(transfer, LockedTransfer):
            log.debug(
                'REGISTERED LOCK node:{} from:{} to:{}'.format(
                    pex(self.our_state.address),
                    pex(from_state.address),
                    pex(to_state.address),
                ),
                lock_amount=transfer.lock.amount,
                lock_expiration=transfer.lock.expiration,
                lock_hashlock=pex(transfer.lock.hashlock),
                hashlock_list=lpex(transfer.lock.hashlock for transfer in to_state.locked.locked.itervalues()),
            )

            to_state.locked.add(transfer)

            # register this channel as waiting for the secret (the secret can
            # be revealed through a message or an blockchain log)
            self.external_state.register_channel_for_hashlock(
                self,
                transfer.lock.hashlock,
            )

        if isinstance(transfer, DirectTransfer) and transfer.secret:
            log.debug(
                'REGISTERED SECRET node:{} from:{} to:{}'.format(
                    pex(self.our_state.address),
                    pex(from_state.address),
                    pex(to_state.address),
                ),
                lock_hashlock=pex(sha3(transfer.secret)),
                lock_secret=pex(transfer.secret),
            )

            to_state.claim_locked(
                from_state,
                transfer.secret,
                transfer.locksroot,
            )

        from_state.transfered_amount = transfer.transfered_amount
        from_state.nonce += 1

        log.debug(
            'REGISTERED TRANSFER node:{} from:{} to:{} '
            'transfer:{} transfered_amount:{} nonce:{} '
            'current_locksroot: {}'.format(
                pex(self.our_state.address),
                pex(from_state.address),
                pex(to_state.address),
                repr(transfer),
                from_state.transfered_amount,
                from_state.nonce,
                pex(to_state.locked.root),
            )
        )

    def create_directtransfer(self, amount, secret=None):
        """ Return a DirectTransfer message.

        This message needs to be signed and registered with the channel before
        sent.
        """
        if not self.isopen:
            raise ValueError('The channel is closed')

        from_ = self.our_state
        to_ = self.partner_state

        distributable = from_.distributable(to_)

        if amount <= 0 or amount > distributable:
            log.debug(
                'Insufficient funds',
                amount=amount,
                distributable=distributable,
            )
            raise ValueError('Insufficient funds')

        # start of critical read section
        transfered_amount = from_.transfered_amount + amount
        current_locksroot = to_.locked.root
        # end of critical read section

        return DirectTransfer(
            nonce=from_.nonce,
            asset=self.asset_address,
            transfered_amount=transfered_amount,
            recipient=to_.address,
            locksroot=current_locksroot,
            secret=secret,
        )

    def create_lockedtransfer(self, amount, expiration, hashlock):
        """ Return a LockedTransfer message.

        This message needs to be signed and registered with the channel before sent.
        """
        if not self.isopen:
            raise ValueError('The channel is closed')

        block_number = self.external_state.get_block_number()

        # expiration is not sufficient for guarantee settling
        if expiration - block_number >= self.settle_timeout:
            log.debug(
                "Transfer expiration doesn't allow for corret settlement.",
                expiration=expiration,
                block_number=block_number,
                settle_timeout=self.settle_timeout,
            )

            raise ValueError('Invalid expiration')

        if expiration - self.reveal_timeout < block_number:
            log.debug(
                'Expiration smaller than the minimum requried.',
                expiration=expiration,
                block_number=block_number,
                reveal_timeout=self.reveal_timeout,
            )

            raise ValueError('Invalid expiration')

        from_ = self.our_state
        to_ = self.partner_state

        distributable = from_.distributable(to_)

        if amount <= 0 or amount > distributable:
            log.debug(
                'Insufficient funds',
                amount=amount,
                distributable=distributable,
            )
            raise ValueError('Insufficient funds')

        lock = Lock(amount, expiration, hashlock)

        # start of critical read section
        transfered_amount = from_.transfered_amount
        updated_locksroot = to_.locked.root_with(lock)
        # end of critical read section

        return LockedTransfer(
            nonce=from_.nonce,
            asset=self.asset_address,
            transfered_amount=transfered_amount,
            recipient=to_.address,
            locksroot=updated_locksroot,
            lock=lock,
        )

    def create_mediatedtransfer(self, transfer_initiator, transfer_target, fee,
                                amount, expiration, hashlock):
        """ Return a MediatedTransfer message.

        This message needs to be signed and registered with the channel before
        sent.

        Args:
            transfer_initiator (address): The node that requested the transfer.
            transfer_target (address): The node that the transfer is destinated to.
            amount (float): How much asset is being transfered.
            expiration (int): The maximum block number until the transfer
                message can be received.
        """

        locked_transfer = self.create_lockedtransfer(
            amount,
            expiration,
            hashlock,
        )

        mediated_transfer = locked_transfer.to_mediatedtransfer(
            transfer_target,
            transfer_initiator,
            fee,
        )
        return mediated_transfer

    def create_refundtransfer_for(self, transfer):
        """ Return RefundTransfer for `transfer`. """
        lock = transfer.lock

        if lock.hashlock not in self.our_state.locked:
            raise ValueError('Unknow hashlock')

        locked_transfer = self.create_lockedtransfer(
            lock.amount,
            lock.expiration,
            lock.hashlock,
        )

        cancel_transfer = locked_transfer.to_refundtransfer()

        return cancel_transfer

    def create_timeouttransfer_for(self, transfer):
        """ Return a TransferTimeout for `transfer`. """
        lock = transfer.lock

        if lock.hashlock not in self.our_state.locked:
            raise ValueError('Unknow hashlock')

        return TransferTimeout(
            transfer.hash,
            lock.hashlock,
        )
