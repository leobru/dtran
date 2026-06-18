(*=r-,p-,t-,s8*)program main(output);
var i:integer; r:real;
_(
i := 25;
r := i;
i := round(r);
i := trunc(r);
if i > r then
    writeln(' greater ', i:5)
else
    writeln(' not greater', r:15:6);
_).
