"""Fixed POSIX exec launcher. Called by the adapter, never by authored code."""
import os
import resource
import sys


def main():
    executable, version = sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
    # Darwin's launcher mappings already exceed 2 GiB (about 392 GiB measured).
    # This is a finite virtual-address cap, not a resident-memory guarantee;
    # GOMEMLIMIT below is only the Go runtime's soft target.
    address_space = (512 if sys.platform == 'darwin' else 2) * 1024**3
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.execve(executable, [executable, '--service=' + version],
              {'PATH': '', 'LANG': 'C.UTF-8', 'GOMEMLIMIT': '128MiB', 'GOMAXPROCS': '2'})


if __name__ == '__main__':
    main()
