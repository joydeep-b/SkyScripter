CC ?= cc
CFLAGS ?= -O3 -Wall -Wextra -std=c11
PKG_CONFIG ?= pkg-config

CFITSIO_CFLAGS := $(shell $(PKG_CONFIG) --cflags cfitsio 2>/dev/null)
CFITSIO_LIBS := $(shell $(PKG_CONFIG) --libs cfitsio 2>/dev/null)

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  OMP_CFLAGS ?= -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include
  OMP_LIBS ?= -L/opt/homebrew/opt/libomp/lib -lomp
else
  OMP_CFLAGS ?= -fopenmp
  OMP_LIBS ?= -fopenmp
endif

TARGETS := local_normalize local_normalize_trimmed_median local_normalize_v2 local_normalize_unregistered

.PHONY: all clean

all: $(TARGETS)

local_normalize: local_normalize.c
	$(CC) $(CFLAGS) $(OMP_CFLAGS) $(CFITSIO_CFLAGS) -o $@ $< $(CFITSIO_LIBS) $(OMP_LIBS) -lm

local_normalize_trimmed_median: local_normalize_trimmed_median.c local_normalize.c
	$(CC) $(CFLAGS) $(OMP_CFLAGS) $(CFITSIO_CFLAGS) -o $@ $< $(CFITSIO_LIBS) $(OMP_LIBS) -lm

local_normalize_v2: local_normalize_v2.c
	$(CC) $(CFLAGS) $(OMP_CFLAGS) $(CFITSIO_CFLAGS) -o $@ $< $(CFITSIO_LIBS) $(OMP_LIBS) -lm

local_normalize_unregistered: local_normalize_unregistered.c
	$(CC) $(CFLAGS) $(OMP_CFLAGS) $(CFITSIO_CFLAGS) -o $@ $< $(CFITSIO_LIBS) $(OMP_LIBS) -lm

clean:
	rm -f $(TARGETS)
