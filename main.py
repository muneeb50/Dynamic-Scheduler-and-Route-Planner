import math
import random
import numpy as np
from GA import block
from GA import RouteManager
from GA import Route
from GA import population
from GA import GA
from GA import Ambulance
from operator import itemgetter


def generateGrid(no_of_blocks, no_of_signals):
    rows = 60
    cols = 60

    # "0" represents Path is Clear
    for i in range(rows):
        for j in range(cols):
            RouteManager.addBlock(block(i * 60 + j, i, j, "0"))

    # "B" represents Path is Blocked
    for i in range(no_of_blocks):
        (RouteManager.getBlock(random.randint(0, RouteManager.numberOfBlocks() - 1))).setValue("B")

    # "S" represents Traffic Signal
    for i in range(no_of_signals):
        (RouteManager.getBlock(random.randint(0, RouteManager.numberOfBlocks() - 1))).setValue("S")


# print 2 best routes
def printPop(pop):
    for i in range(2):
        route = pop.getRoute(i)
        temp = []
        indexes = []

        for j in range(route.routeSize()):
            temp.append(route.getBlock(j).getValue())
            indexes.append(route.getBlock(j).getName())

        print("\nRoute_", i, ": ", temp)
        print("Indices of Route (in Grid)", indexes)
        print("Fitness: ", route.getFitness())
        print("Distance: ", route.getDistance())


# Get 2 Best Routes out of all calculated Routes of Population
def getRoutes(pop, noOfRoutes):
    routesPop = population(2, False)
    if pop.populationSize() >= noOfRoutes:
        for i in range(noOfRoutes):
            routesPop.saveRoute(i, pop.getRoute(i))
        return routesPop

    return None


def start_Genetic_Algorithm(st, goal):
    startBlock = st
    destBlock = goal

    # to increase accuracy, increase "iterations"
    chromosomeSize = 20
    iterations = 10

    pop = population(20, True, startBlock, destBlock, chromosomeSize)
    print("Initial distance (Before Genetic Algorithm): ", pop.getFittest().getDistance())

    pop = GA.evolvePopulation(pop)

    for i in range(iterations):
        pop = GA.evolvePopulation(pop)

    return pop


# schedule ambulances based on Severity of Patients
def scheduling_ambulance(amb, calls):
    calls = sorted(calls, key=itemgetter(1), reverse=True)

    for i in range(amb.__len__()):
        if amb[i].getisFree() is True:
            amb[i].setStart(calls[i][0][0])
            amb[i].setEnd(calls[i][0][1])
            amb[i].setisFree(False)
            print("\nAmbulance :", i)
            y = start_Genetic_Algorithm(amb[i].getStart(), amb[i].getEnd())
            amb[i].setAllRoutes(getRoutes(y, 2))


##------------------MAIN Program Start here-----------------##

# initialize grid of 1000x500
generateGrid(1000, 500)

# initialize Patient calls for Ambulance
# Syntax --> [start_index_of _grid, goal_index_of_grid], Severity 1 to 5 : 5 being Highest
patientCalls = []
patientCalls.append([[2, 1500], 1])
patientCalls.append([[10, 1000], 2])
patientCalls.append([[100, 2000], 2])
patientCalls.append([[500, 3555], 3])
patientCalls.append([[2000, 3500], 4])
patientCalls.append([[2500, 3000], 5])

# initialize Ambulances available for today
amb = []
amb.append(Ambulance())
amb.append(Ambulance())
amb.append(Ambulance())
amb.append(Ambulance())
amb.append(Ambulance())

# Schedule Ambulances based on Severity of Patients
scheduling_ambulance(amb, patientCalls)

# Get Optimal Routes Calculated by Genetic Algorithms
for x in range(5):
    print("\n\n--> Ambulance: ", x)
    printPop(amb[x].getAllRoutes())
