import argparse
def main(argv=None):
 p=argparse.ArgumentParser(); p.add_argument('command',nargs='?'); p.parse_args(argv); return 0
if __name__=='__main__': main()
