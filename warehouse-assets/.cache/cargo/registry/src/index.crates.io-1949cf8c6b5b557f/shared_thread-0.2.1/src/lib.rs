//! This crate provides [`SharedThread`], a wrapper around
//! [`std::thread::JoinHandle`](https://doc.rust-lang.org/std/thread/struct.JoinHandle.html) that
//! lets multiple threads wait on a shared thread and read its output, with an optional timeout.
//!
//! For example code, see [the `SharedThread` example](struct.SharedThread.html#example).

#![deny(unsafe_code)]

use std::fmt;
use std::mem;
use std::panic::{catch_unwind, resume_unwind, AssertUnwindSafe};
use std::sync::MutexGuard;
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::thread;
use std::time::Duration;
use std::time::Instant;

/// A wrapper around [`std::thread::JoinHandle`] that allows for multiple waiters.
///
/// The high-level differences between `SharedThread` and [`JoinHandle`] are:
///
/// - [`join`][SharedThread::join] takes `&self` rather than `&mut self`.
/// - [`join`][SharedThread::join] returns `&T` rather than `T`. For taking ownership of `T`, see
///   [`into_output`][SharedThread::into_output].
/// - `SharedThread` also provides [`join_timeout`][Self::join_timeout],
///   [`join_deadline`][Self::join_deadline], and [`try_join`][SharedThread::try_join].
/// - Rather than converting panics into
///   [`std::thread::Result`](https://doc.rust-lang.org/std/thread/type.Result.html), which usually
///   requires the caller to `.unwrap()` every `.join()`, `SharedThread` propagates panics
///   automatically.
///
/// # Example
///
/// ```
/// use shared_thread::SharedThread;
/// use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
///
/// // Use this flag to tell our shared thread when to stop.
/// static EXIT_FLAG: AtomicBool = AtomicBool::new(false);
///
/// // Start a background thread that we'll share with several waiting threads.
/// let shared_thread = SharedThread::spawn(|| {
///     // Pretend this is some expensive, useful background work...
///     while (!EXIT_FLAG.load(Relaxed)) {}
///
///     42
/// });
///
/// // It's up to you how to share the SharedThread object with other threads. In this sense it's
/// // like any other object you might need to share, like say a HashMap or a File. The common
/// // options are to put it in an Arc, or to let "scoped" threads borrow it directly. Let's use
/// // scoped threads.
/// std::thread::scope(|scope| {
///     // Spawn three waiter threads that each wait on the shared thread.
///     let waiter1 = scope.spawn(|| shared_thread.join());
///     let waiter2 = scope.spawn(|| shared_thread.join());
///     let waiter3 = scope.spawn(|| shared_thread.join());
///
///     // In this example, the shared thread is going to keep looping until we set the EXIT_FLAG.
///     // In the meantime, .is_finished() returns false, and .try_join() returns None.
///     assert!(!shared_thread.is_finished());
///     assert_eq!(shared_thread.try_join(), None);
///
///     // Ask the shared thread to stop looping.
///     EXIT_FLAG.store(true, Relaxed);
///
///     // Now all the calls to .join() above will return quickly, and each of the waiter threads
///     // will get a reference to the shared thread's output, &42.
///     assert_eq!(*waiter1.join().unwrap(), 42);
///     assert_eq!(*waiter2.join().unwrap(), 42);
///     assert_eq!(*waiter3.join().unwrap(), 42);
///
///     // Now that the shared thread has finished, .is_finished() returns true, and .try_join()
///     // returns Some(&42).
///     assert!(shared_thread.is_finished());
///     assert_eq!(*shared_thread.try_join().unwrap(), 42);
/// });
///
///  // We can take ownership of the output by consuming the SharedThread object. As with any
///  // non-Copy type in Rust, this requires that the SharedThread is not borrowed.
///  assert_eq!(shared_thread.into_output(), 42);
/// ```
///
/// [`std::thread::JoinHandle`]: https://doc.rust-lang.org/std/thread/struct.JoinHandle.html
/// [`JoinHandle`]: https://doc.rust-lang.org/std/thread/struct.JoinHandle.html
#[derive(Debug)]
pub struct SharedThread<T> {
    state: Mutex<State<T>>,
    exit_signal: Arc<ExitSignal>,
    output: OnceLock<T>,
}

// The shared thread sets this bool to true and signals the condvar when it exits, even if it
// panics.
#[derive(Debug)]
struct ExitSignal {
    mutex: Mutex<bool>,
    condvar: Condvar,
}

enum State<T> {
    Running(thread::JoinHandle<T>),
    // Note that the return value T goes in the OnceLock. If it lived here in the Exited variant,
    // it would be stuck inside the state Mutex, and we couldn't share it with simple references.
    Exited,
    Panicked,
}

use State::*;

impl<T> fmt::Debug for State<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Running { .. } => write!(f, "Running"),
            Exited => write!(f, "Exited"),
            Panicked => write!(f, "Panicked"),
        }
    }
}

impl<T: Send + 'static> SharedThread<T> {
    /// Spawn a new `SharedThread`.
    ///
    /// # Panics
    ///
    /// This function calls
    /// [`std::thread::spawn`](https://doc.rust-lang.org/std/thread/fn.spawn.html) internally,
    /// which panics if it fails to spawn a thread.
    pub fn spawn<F>(f: F) -> Self
    where
        F: FnOnce() -> T + Send + 'static,
    {
        let exit_signal = Arc::new(ExitSignal {
            mutex: Mutex::new(false),
            condvar: Condvar::new(),
        });
        let exit_signal_clone = Arc::clone(&exit_signal);
        let handle = thread::spawn(move || {
            // Whether or not the closure `f` panics, set the exited flag and notify the condvar.
            // It's not clear to me that the concept of "unwind safety" in the standard library was
            // a good indea, but at least it doesn't require any unsafe code to work around it.
            let unwind_result = catch_unwind(AssertUnwindSafe(f));
            let mut guard = lock_ignoring_poison(&exit_signal_clone.mutex);
            *guard = true;
            exit_signal_clone.condvar.notify_all();
            // Now that we've signaled exit, if there was a panic, propagate it. The first waiter
            // thread will observe it. (Subsequent waiter threads will only see the Panicked state
            // variant.)
            match unwind_result {
                Ok(return_value) => return_value,
                Err(panic) => resume_unwind(panic),
            }
        });
        SharedThread {
            state: Mutex::new(Running(handle)),
            exit_signal,
            output: OnceLock::new(),
        }
    }
}

// A thread that multiple other threads can wait on simultaneously.
impl<T> SharedThread<T> {
    fn join_exited_thread(&self, exit_signal_guard: MutexGuard<bool>) -> &T {
        // Release the `exit_signal` lock before taking the `state` lock. This guarantees that even
        // in the rare case where long-running thread-local destructors make the following join
        // take a long time, we don't block `is_finished`.
        debug_assert!(*exit_signal_guard, "the thread closure has finished");
        drop(exit_signal_guard);

        let mut state_guard = lock_ignoring_poison(&self.state);
        match &*state_guard {
            // Running means we're the thread that needs to .join(). Fall through.
            Running(_) => {}
            // Exited or Panicked means someone already joined.
            Exited => return self.output.get().unwrap(),
            Panicked => panic!("shared thread panicked"),
        };

        // We need to .join(), so take the JoinHandle by value. Use the Panicked state as a
        // placeholder, so that it's the state we leave behind if something does in fact panic.
        // This makes the Panicked state kind of ambiguous between "the other thread panicked" or
        // "we failed an assert somewhere", but at least the initial panic backtrace will make it
        // clear what happened.
        let Running(handle) = mem::replace(&mut *state_guard, Panicked) else {
            unreachable!()
        };

        // The thread has signaled that it's exiting, so .join() will almost always return quickly.
        // If the thread panicked, propagate the panic.
        //
        // NB: .join() might not return quickly if a thread-local variable has a Drop
        // implementation that takes a long time. We could consider using a thread-local variable
        // ourselves to signal thread exit, but there are several problems with that.
        // - It only works if we can assume LIFO order of thread-local destructors, which is common but not
        //   universal.
        // - It also exposes us to platform-specific cases where thread-local destructors
        //   might never run.
        // - Even if thread-local destructors run reliably and in the order we want, other
        //   platform-specific hooks could still run afterwards.
        //
        // Note that `JoinHandle::is_finished` has essentially the same issue.
        match handle.join() {
            Ok(return_value) => {
                // Because we set `output` while we hold the state mutex, it's guaranteed that
                // subsequent threads that see the Exited state will also see that `output` is set.
                let set_result = self.output.set(return_value);
                assert!(set_result.is_ok(), "output must be previously unset");
                *state_guard = Exited;
                self.output.get().unwrap()
            }
            Err(panic) => resume_unwind(panic),
        }
    }

    /// Wait for the shared thread to finish, then return `&T`. This blocks the current thread.
    ///
    /// # Panics
    ///
    /// This function panics if the shared thread panicked. The original panic is propagated
    /// directly with [`resume_unwind`](https://doc.rust-lang.org/std/panic/fn.resume_unwind.html)
    /// the first time. Subsequent calls panic with a generic message.
    pub fn join(&self) -> &T {
        let mut exit_signal_guard = lock_ignoring_poison(&self.exit_signal.mutex);
        while !*exit_signal_guard {
            exit_signal_guard = wait_ignoring_poison(&self.exit_signal.condvar, exit_signal_guard);
        }
        self.join_exited_thread(exit_signal_guard)
    }

    /// Wait with a timeout for the shared thread to finish. If it finishes in time (or it already
    /// finished), return `Some(&T)`, otherwise return `None`. This blocks the current thread.
    ///
    /// This function can exceed its timeout in the rare case that a [`thread_local`] variable has
    /// a [`Drop`] implementation that takes a long time. Platform-specific hooks like
    /// [`pthread_key_create`] can also have a similar effect. Note that
    /// [`JoinHandle::is_finished`] has the same limitation.
    ///
    /// [`thread_local`]: https://doc.rust-lang.org/std/macro.thread_local.html
    /// [`Drop`]: https://doc.rust-lang.org/std/ops/trait.Drop.html
    /// [`pthread_key_create`]: https://man7.org/linux/man-pages/man3/pthread_key_create.3.html
    /// [`JoinHandle::is_finished`]: https://doc.rust-lang.org/std/thread/struct.JoinHandle.html#method.is_finished
    ///
    /// # Panics
    ///
    /// This function panics if the shared thread panicked. The original panic is propagated
    /// directly with [`resume_unwind`](https://doc.rust-lang.org/std/panic/fn.resume_unwind.html)
    /// the first time it's observed. Subsequent calls panic with a generic message.
    ///
    /// This function also panics if `Instant::now() + timeout` overflows.
    pub fn join_timeout(&self, timeout: Duration) -> Option<&T> {
        // `Instant` doesn't currently support saturating operations, so this addition can panic.
        // See https://internals.rust-lang.org/t/instant-systemtime-min-max/21375.
        let deadline = Instant::now() + timeout;
        self.join_deadline(deadline)
    }

    /// Wait with a deadline for the shared thread to finish. If it finishes in time (or it already
    /// finished), return `Some(&T)`, otherwise return `None`. This blocks the current thread.
    ///
    /// This function can exceed its deadline in the rare case that a [`thread_local`] variable has
    /// a [`Drop`] implementation that takes a long time. Platform-specific hooks like
    /// [`pthread_key_create`] can also have a similar effect. Note that
    /// [`JoinHandle::is_finished`] has the same limitation.
    ///
    /// [`thread_local`]: https://doc.rust-lang.org/std/macro.thread_local.html
    /// [`Drop`]: https://doc.rust-lang.org/std/ops/trait.Drop.html
    /// [`pthread_key_create`]: https://man7.org/linux/man-pages/man3/pthread_key_create.3.html
    /// [`JoinHandle::is_finished`]: https://doc.rust-lang.org/std/thread/struct.JoinHandle.html#method.is_finished
    ///
    /// # Panics
    ///
    /// This function panics if the shared thread panicked. The original panic is propagated
    /// directly with [`resume_unwind`](https://doc.rust-lang.org/std/panic/fn.resume_unwind.html)
    /// the first time it's observed. Subsequent calls panic with a generic message.
    pub fn join_deadline(&self, deadline: Instant) -> Option<&T> {
        let mut exit_signal_guard = lock_ignoring_poison(&self.exit_signal.mutex);
        while !*exit_signal_guard {
            if Instant::now() > deadline {
                return None;
            }
            exit_signal_guard = wait_deadline_ignoring_poison(
                &self.exit_signal.condvar,
                exit_signal_guard,
                deadline,
            );
        }
        Some(self.join_exited_thread(exit_signal_guard))
    }

    /// Return `Some(&T)` if the shared thread has already finished, otherwise `None`.
    ///
    /// This function almost always returns quickly. The exception is if a [`thread_local`]
    /// variable has a [`Drop`] implementation that takes a long time. Platform-specific hooks like
    /// [`pthread_key_create`] can also have a similar effect. Note that
    /// [`JoinHandle::is_finished`] has the same limitation.
    ///
    /// [`thread_local`]: https://doc.rust-lang.org/std/macro.thread_local.html
    /// [`Drop`]: https://doc.rust-lang.org/std/ops/trait.Drop.html
    /// [`pthread_key_create`]: https://man7.org/linux/man-pages/man3/pthread_key_create.3.html
    /// [`JoinHandle::is_finished`]: https://doc.rust-lang.org/std/thread/struct.JoinHandle.html#method.is_finished
    ///
    /// # Panics
    ///
    /// This function panics if the shared thread panicked. The original panic is propagated
    /// directly with [`resume_unwind`](https://doc.rust-lang.org/std/panic/fn.resume_unwind.html)
    /// the first time it's observed. Subsequent calls panic with a generic message.
    pub fn try_join(&self) -> Option<&T> {
        let exit_signal_guard = lock_ignoring_poison(&self.exit_signal.mutex);
        if *exit_signal_guard {
            Some(self.join_exited_thread(exit_signal_guard))
        } else {
            None
        }
    }

    /// Wait for the shared thread to finish, then return `T`. This blocks the current thread. This
    /// requires ownership of the `SharedThread` and consumes it.
    ///
    /// # Panics
    ///
    /// This function panics if the shared thread panicked. The original panic is propagated
    /// directly with [`resume_unwind`](https://doc.rust-lang.org/std/panic/fn.resume_unwind.html)
    /// the first time it's observed. Subsequent calls panic with a generic message.
    pub fn into_output(self) -> T {
        self.join();
        self.output.into_inner().expect("should be set")
    }

    /// Return `true` if the shared thread has finished, `false` otherwise.
    ///
    /// This function never blocks. If it returns `true`, [`try_join`][Self::try_join],
    /// [`join_timeout`][Self::join_timeout], and [`join_deadline`][Self::join_deadline] are
    /// guaranteed not to return `None`, and all the join functions will generally return quickly.
    /// The exception is if a [`thread_local`] variable has a [`Drop`] implementation that takes a
    /// long time. Platform-specific hooks like [`pthread_key_create`] can also have a similar
    /// effect. Note that [`JoinHandle::is_finished`] has the same limitation.
    ///
    /// [`thread_local`]: https://doc.rust-lang.org/std/macro.thread_local.html
    /// [`Drop`]: https://doc.rust-lang.org/std/ops/trait.Drop.html
    /// [`pthread_key_create`]: https://man7.org/linux/man-pages/man3/pthread_key_create.3.html
    /// [`JoinHandle::is_finished`]: https://doc.rust-lang.org/std/thread/struct.JoinHandle.html#method.is_finished
    pub fn is_finished(&self) -> bool {
        *lock_ignoring_poison(&self.exit_signal.mutex)
    }
}

fn lock_ignoring_poison<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    match mutex.lock() {
        Ok(guard) => guard,
        Err(e) => e.into_inner(),
    }
}

fn wait_ignoring_poison<'guard, T>(
    condvar: &Condvar,
    guard: MutexGuard<'guard, T>,
) -> MutexGuard<'guard, T> {
    match condvar.wait(guard) {
        Ok(guard) => guard,
        Err(e) => e.into_inner(),
    }
}

fn wait_deadline_ignoring_poison<'guard, T>(
    condvar: &Condvar,
    guard: MutexGuard<'guard, T>,
    deadline: Instant,
) -> MutexGuard<'guard, T> {
    let timeout = deadline.saturating_duration_since(Instant::now());
    match condvar.wait_timeout(guard, timeout) {
        Ok((guard, _)) => guard,
        Err(e) => e.into_inner().0,
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering::Relaxed};

    #[test]
    fn test_join_and_try_join() {
        static STOP_FLAG: AtomicBool = AtomicBool::new(false);
        let bg_thread = SharedThread::spawn(|| {
            while !STOP_FLAG.load(Relaxed) {}
            42
        });
        // Spawn 10 joiner threads that all simultaneously try to join the background thread.
        thread::scope(|scope| {
            let mut joiner_handles = Vec::new();
            for _ in 0..10 {
                joiner_handles.push(scope.spawn(|| {
                    bg_thread.join();
                }));
            }
            // try_join will always return None here.
            for _ in 0..100 {
                assert!(bg_thread.try_join().is_none());
                assert!(!bg_thread.is_finished());
            }
            STOP_FLAG.store(true, Relaxed);
            // One of the joiner threads almost certainly has the underlying thread handle, and
            // eventually it'll set SharedThread::result and one of these try_joins will return Some.
            while !bg_thread.is_finished() {}
            assert_eq!(bg_thread.try_join(), Some(&42));
        });
    }

    #[test]
    fn test_try_join_only() {
        static STOP_FLAG: AtomicBool = AtomicBool::new(false);
        let bg_thread = SharedThread::spawn(|| {
            while !STOP_FLAG.load(Relaxed) {}
            42
        });
        // try_join will always return None here.
        for _ in 0..100 {
            assert!(bg_thread.try_join().is_none());
        }
        STOP_FLAG.store(true, Relaxed);
        // Eventually one of these try_join's will see .is_finished() = true and join the thread.
        while bg_thread.try_join().is_none() {}
        assert_eq!(bg_thread.try_join(), Some(&42));
    }

    #[test]
    fn test_into_inner() {
        let thread = SharedThread::spawn(|| String::from("foo"));
        let result: String = thread.into_output();
        assert_eq!(result, "foo");
    }

    #[test]
    fn test_panic_messages() {
        let thread = SharedThread::spawn(|| panic!("original message"));
        let panic_error = catch_unwind(|| thread.join()).unwrap_err();
        assert_eq!(panic_error.downcast_ref(), Some(&"original message"));

        let second_panic_error = catch_unwind(|| thread.join()).unwrap_err();
        assert_eq!(
            second_panic_error.downcast_ref(),
            Some(&"shared thread panicked"),
        );
    }

    #[test]
    fn test_is_finished_never_blocks() {
        // There's a corner case that affects all the join methods: If a `thread_local!` variable
        // has a `Drop` implementation that takes a long time, it can delay thread exit even after
        // we've set the `ExitSignal` to true. We currently allow that case to exceed timeouts, but
        // we don't allow it to block `is_finished()`.
        //
        // XXX: This sort of thing blocked process exit on Windows until Rust 1.98. See
        // https://github.com/rust-lang/rust/pull/148799.
        struct DropBlocksForever {}
        impl Drop for DropBlocksForever {
            fn drop(&mut self) {
                std::thread::sleep(Duration::from_secs(1_000_000));
            }
        }
        thread_local! {
            static FOREVER: DropBlocksForever = DropBlocksForever {};
        }
        let thread = Arc::new(SharedThread::spawn(|| {
            // Thread locals are lazily initialized, so force initialization.
            FOREVER.with(|_| {});
        }));
        // Spawn another thread that will try to join and take the `exit_signal` lock.
        std::thread::spawn({
            let thread = Arc::clone(&thread);
            move || {
                thread.join();
            }
        });
        // Give that thread some time to get to the join. Use an excessively long time to reduce
        // the chance of a CI flake.
        std::thread::sleep(Duration::from_secs(1));
        // Ok, now, check `is_finished`. It should return true. More importantly, it should return
        // quickly. What we're testing is that that other thread isn't holding the `exit_signal`
        // lock across its blocking wait. You can make this test block "forever" by commenting out
        // `drop(exit_signal_guard)` at the top of `join_exited_thread`.
        assert!(thread.is_finished());
    }
}
