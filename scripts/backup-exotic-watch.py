"""Daily source-only Exotic backup; --snapshot DIR is offline and never pushes."""
from monitor_backup import backup_main
if __name__ == '__main__':
    backup_main('exotic')
