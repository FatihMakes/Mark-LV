#include "WorldStateSubsystem.h"
#include "WorldStateSaveGame.h"
#include "Kismet/GameplayStatics.h"

const TCHAR* UWorldStateSubsystem::SaveSlotName = TEXT("WorldDestructionState");

void UWorldStateSubsystem::MarkBuildingDestroyed(FName BuildingId)
{
	if (!BuildingId.IsNone())
	{
		DestroyedBuildingIds.Add(BuildingId);
	}
}

bool UWorldStateSubsystem::WasBuildingDestroyed(FName BuildingId) const
{
	return DestroyedBuildingIds.Contains(BuildingId);
}

bool UWorldStateSubsystem::SaveWorldState(FString& OutError)
{
	UWorldStateSaveGame* SaveObj = Cast<UWorldStateSaveGame>(UGameplayStatics::CreateSaveGameObject(UWorldStateSaveGame::StaticClass()));
	if (!SaveObj)
	{
		OutError = TEXT("Failed to create world state save object.");
		return false;
	}

	SaveObj->DestroyedBuildingIds = DestroyedBuildingIds.Array();
	if (!UGameplayStatics::SaveGameToSlot(SaveObj, SaveSlotName, 0))
	{
		OutError = TEXT("Failed to save world destruction state.");
		return false;
	}

	OutError.Reset();
	return true;
}

bool UWorldStateSubsystem::LoadWorldState(FString& OutError)
{
	if (!UGameplayStatics::DoesSaveGameExist(SaveSlotName, 0))
	{
		OutError = TEXT("No world destruction save exists.");
		return false;
	}

	UWorldStateSaveGame* SaveObj = Cast<UWorldStateSaveGame>(UGameplayStatics::LoadGameFromSlot(SaveSlotName, 0));
	if (!SaveObj)
	{
		OutError = TEXT("World destruction save data is invalid.");
		return false;
	}

	DestroyedBuildingIds.Reset();
	DestroyedBuildingIds.Append(SaveObj->DestroyedBuildingIds);

	OutError.Reset();
	return true;
}

void UWorldStateSubsystem::ResetWorldState()
{
	DestroyedBuildingIds.Reset();
}
