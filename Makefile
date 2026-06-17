CXX ?= g++
CXXFLAGS ?= -O2 -Wall

# Combined disassembler (DMS + Pascal-A + Pascal-B).
dtran: dtran.cc
	$(CXX) $(CXXFLAGS) -o $@ $<

clean:
	rm -f dtran 

.PHONY: clean
